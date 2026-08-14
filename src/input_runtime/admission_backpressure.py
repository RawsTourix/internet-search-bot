"""Corrective bounded backlog handling for committed-but-unadmitted input."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Iterable
from typing import Any

from src.input_runtime.admission import InputAdmissionAction, InputAdmissionOutcome
from src.input_runtime.composition import register_input_runtime_binding
from src.input_runtime.ir7_admission_reclassification import (
    InputAdmissionService as _IR7InputAdmissionService,
)
from src.input_runtime.models import CheckpointAction, CheckpointOutcome


logger = logging.getLogger("InputRuntime.Backpressure")


class _RecoveryDeferredCheckpointService:
    """Refill deferred committed input only at existing safe checkpoints."""

    def __init__(self, delegate: Any, admission_service: "InputAdmissionService") -> None:
        self._delegate = delegate
        self._admission_service = admission_service

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    @staticmethod
    def _interrupt(outcome: CheckpointOutcome, reason_code: str) -> CheckpointOutcome:
        return CheckpointOutcome(
            checkpoint=outcome.checkpoint,
            action=CheckpointAction.INTERRUPT,
            context_revision_id=outcome.context_revision_id,
            applied_through_cycle_sequence=(
                outcome.applied_through_cycle_sequence
            ),
            applied_input_batch_ids=outcome.applied_input_batch_ids,
            reason_code=reason_code,
        )

    async def run_checkpoint(self, **kwargs: Any) -> CheckpointOutcome:
        active_cycle = kwargs.get("active_cycle")
        apply_input = bool(kwargs.get("apply_input", True))
        session_id = (
            str(getattr(active_cycle, "session_id", "") or "")
            if active_cycle is not None
            else ""
        )
        cycle_id = (
            str(getattr(active_cycle, "cycle_id", "") or "")
            if active_cycle is not None
            else ""
        )
        generation = (
            int(getattr(active_cycle, "input_runtime_generation", 0))
            if active_cycle is not None
            else 0
        )

        if apply_input and session_id and cycle_id:
            try:
                await self._admission_service.drain_recovery_deferred(
                    session_id,
                    expected_cycle_id=cycle_id,
                    expected_generation=generation,
                )
            except Exception:
                logger.exception(
                    "recovery_deferred_pre_checkpoint_admission_failed "
                    "session_id=%s cycle_id=%s",
                    session_id,
                    cycle_id,
                )

        outcome = await self._delegate.run_checkpoint(**kwargs)
        if (
            not apply_input
            or not session_id
            or not cycle_id
            or outcome.action
            in {
                CheckpointAction.INTERRUPT,
                CheckpointAction.PAUSE,
                CheckpointAction.WAIT,
                CheckpointAction.ABORT_FINALIZATION,
            }
            or self._admission_service.recovery_deferred_count(session_id) == 0
        ):
            return outcome

        try:
            admitted = await self._admission_service.drain_recovery_deferred(
                session_id,
                expected_cycle_id=cycle_id,
                expected_generation=generation,
            )
        except Exception:
            logger.exception(
                "recovery_deferred_post_checkpoint_admission_failed "
                "session_id=%s cycle_id=%s",
                session_id,
                cycle_id,
            )
            return self._interrupt(
                outcome,
                "recovery_deferred_admission_failed",
            )

        if (
            self._admission_service.recovery_deferred_count(session_id) > 0
            and not admitted
        ):
            # The checkpoint has already consumed its entry watermark. If no
            # deferred head can now be admitted, continuing could eventually
            # permit stale waiting/terminal authority while durable input is
            # still outside the runtime relation.
            return self._interrupt(
                outcome,
                "recovery_deferred_admission_stalled",
            )
        return outcome


class InputAdmissionService(_IR7InputAdmissionService):
    """Preserve FIFO priority for startup-deferred committed batches.

    Durable committed storage remains the source of truth. This layer keeps only
    exact batch IDs in process memory after startup discovery; a restart rebuilds
    the backlog from durable committed/admission state.
    """

    def __init__(self, **kwargs: Any) -> None:
        self._recovery_deferred: dict[str, deque[str]] = {}
        self._recovery_backpressure_locks: dict[str, asyncio.Lock] = {}
        super().__init__(**kwargs)

        delegate = self.checkpoint_service
        self.checkpoint_service = _RecoveryDeferredCheckpointService(
            delegate,
            self,
        )
        # IR-4 registers the binding before this corrective wrapper exists.
        # Replace only the process-local application binding; durable authority
        # and all repository/service instances remain unchanged.
        self.application_binding = register_input_runtime_binding(
            config=self.config,
            repositories=self.repositories,
            committed_batches=self.committed_batches,
            checkpoint_service=self.checkpoint_service,
            emission_service=self.emission_service,
            emission_outbox_service=self.emission_outbox_service,
            finalization_service=self.finalization_service,
        )

    def _backpressure_lock(self, session_id: str) -> asyncio.Lock:
        return self._recovery_backpressure_locks.setdefault(
            session_id,
            asyncio.Lock(),
        )

    def install_recovery_deferred(
        self,
        entries: Iterable[tuple[str, str]],
    ) -> None:
        """Install the exact startup-discovered backlog before readiness opens."""

        grouped: dict[str, deque[str]] = {}
        seen: dict[str, set[str]] = {}
        for raw_session_id, raw_input_batch_id in entries:
            session_id = str(raw_session_id).strip()
            input_batch_id = str(raw_input_batch_id).strip()
            if not session_id or not input_batch_id:
                raise ValueError("deferred recovery identities must not be empty")
            known = seen.setdefault(session_id, set())
            if input_batch_id in known:
                continue
            known.add(input_batch_id)
            grouped.setdefault(session_id, deque()).append(input_batch_id)
        self._recovery_deferred = grouped

    def recovery_deferred_count(self, session_id: str) -> int:
        queue = self._recovery_deferred.get(session_id.strip())
        return len(queue) if queue is not None else 0

    def recovery_deferred_ids(self, session_id: str) -> tuple[str, ...]:
        queue = self._recovery_deferred.get(session_id.strip())
        return tuple(queue) if queue is not None else ()

    def clear_recovery_deferred_session(self, session_id: str) -> int:
        """Drop process-local backlog after durable lifecycle expiration/reset."""

        queue = self._recovery_deferred.pop(session_id.strip(), None)
        return len(queue) if queue is not None else 0

    async def _discard_already_admitted_heads_locked(self, session_id: str) -> None:
        queue = self._recovery_deferred.get(session_id)
        while queue:
            existing = await self.repositories.admissions.get_by_input_batch_id(
                queue[0]
            )
            if existing is None:
                break
            queue.popleft()
        if queue is not None and not queue:
            self._recovery_deferred.pop(session_id, None)

    @staticmethod
    def _backlog_blocked(
        *,
        input_batch_id: str,
        session_id: str,
    ) -> InputAdmissionOutcome:
        return InputAdmissionOutcome(
            input_batch_id=input_batch_id,
            session_id=session_id,
            action=InputAdmissionAction.CAPACITY_BLOCKED,
            should_start_runner=False,
            should_wake_runner=False,
            user_projection_key="input_runtime.admission.capacity_blocked",
            retryable=True,
            reason_code="recovery_deferred_backlog",
        )

    async def admit_committed_batch(
        self,
        input_batch_id: str,
        *,
        session_id: str,
    ) -> InputAdmissionOutcome:
        input_batch_id = input_batch_id.strip()
        session_id = session_id.strip()
        if not input_batch_id or not session_id:
            raise ValueError("input_batch_id and session_id are required")

        async with self._backpressure_lock(session_id):
            await self._discard_already_admitted_heads_locked(session_id)
            queue = self._recovery_deferred.get(session_id)
            if queue and input_batch_id != queue[0]:
                existing = (
                    await self.repositories.admissions.get_by_input_batch_id(
                        input_batch_id
                    )
                )
                if existing is None:
                    return self._backlog_blocked(
                        input_batch_id=input_batch_id,
                        session_id=session_id,
                    )

            outcome = await super().admit_committed_batch(
                input_batch_id,
                session_id=session_id,
            )
            queue = self._recovery_deferred.get(session_id)
            if (
                queue
                and queue[0] == input_batch_id
                and outcome.action != InputAdmissionAction.CAPACITY_BLOCKED
            ):
                queue.popleft()
                if not queue:
                    self._recovery_deferred.pop(session_id, None)
            return outcome

    async def drain_recovery_deferred(
        self,
        session_id: str,
        *,
        expected_cycle_id: str | None = None,
        expected_generation: int | None = None,
    ) -> tuple[InputAdmissionOutcome, ...]:
        """Admit oldest deferred batches until bounded capacity blocks again."""

        session_id = session_id.strip()
        if not session_id:
            raise ValueError("session_id is required")

        admitted: list[InputAdmissionOutcome] = []
        async with self._backpressure_lock(session_id):
            await self._discard_already_admitted_heads_locked(session_id)
            while self._recovery_deferred.get(session_id):
                if expected_cycle_id is not None:
                    state = await self.repositories.sessions.get(session_id)
                    if (
                        state is None
                        or state.active_cycle_id != expected_cycle_id
                        or (
                            expected_generation is not None
                            and state.generation != expected_generation
                        )
                    ):
                        break

                queue = self._recovery_deferred[session_id]
                input_batch_id = queue[0]
                outcome = await super().admit_committed_batch(
                    input_batch_id,
                    session_id=session_id,
                )
                if outcome.action == InputAdmissionAction.CAPACITY_BLOCKED:
                    break
                if (
                    expected_cycle_id is not None
                    and outcome.admission is not None
                    and (
                        outcome.admission.target_cycle_id != expected_cycle_id
                        or (
                            expected_generation is not None
                            and outcome.admission.admitted_generation
                            != expected_generation
                        )
                    )
                ):
                    raise RuntimeError(
                        "deferred recovery admission changed active-cycle authority"
                    )
                queue.popleft()
                admitted.append(outcome)
                if not queue:
                    self._recovery_deferred.pop(session_id, None)
                    break
        return tuple(admitted)