"""Capacity- and lifecycle-aware startup recovery for committed durable input."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from src.input_runtime.admission import InputAdmissionAction, InputAdmissionOutcome
from src.input_runtime.models import AdmissionState, CycleStatus
from src.input_runtime.recovery import RecoveryDisposition, RecoverySessionPlan
from src.input_runtime.recovery_terminal import (
    InputRuntimeRecoveryCoordinator as _TerminalRecoveryCoordinator,
)


logger = logging.getLogger("InputRuntime.Recovery.Backpressure")

_RECOVERY_EXPIRY_REASON = "recovery_window_expired"
_FUTURE_CLOCK_SKEW_TOLERANCE = timedelta(minutes=5)
_TERMINAL_OR_IDLE = {
    CycleStatus.IDLE,
    CycleStatus.DONE,
    CycleStatus.ERROR,
    CycleStatus.CANCELLED,
}


class InputRuntimeRecoveryCoordinator(_TerminalRecoveryCoordinator):
    """Keep startup recovery bounded and refuse stale automatic execution.

    Automatic recovery age is anchored to immutable committed-input timestamps.
    Recovery-written session/snapshot timestamps are deliberately excluded so a
    failed restart cannot make old work appear fresh on the next restart.
    """

    async def _recover(self, report):
        batches = await self.committed_batches.list_committed_for_recovery()
        self._recovery_committed_batches = tuple(batches)
        self._recovery_activity_at: dict[str, datetime] = {}
        for batch in batches:
            session_id = str(batch.session_id)
            committed_at = self._normalize_activity_timestamp(
                getattr(batch, "committed_at", None)
            )
            previous = self._recovery_activity_at.get(session_id)
            if previous is None or committed_at > previous:
                self._recovery_activity_at[session_id] = committed_at
        return await super()._recover(report)

    @classmethod
    def _normalize_activity_timestamp(cls, value) -> datetime:
        if not isinstance(value, datetime):
            raise cls._fatal("recovery_activity_timestamp_missing")
        if value.tzinfo is None or value.utcoffset() is None:
            raise cls._fatal("recovery_activity_timestamp_not_timezone_aware")
        return value.astimezone(timezone.utc)

    def _auto_resume_max_age_seconds(self) -> int:
        config = getattr(self.admission_service, "config", None)
        return int(
            getattr(config, "recovery_auto_resume_max_age_seconds", 21_600)
        )

    def _activity_expired(
        self,
        reference_at: datetime | None,
        *,
        now: datetime,
    ) -> bool:
        max_age = self._auto_resume_max_age_seconds()
        if max_age == 0 or reference_at is None:
            return True
        if reference_at > now + _FUTURE_CLOCK_SKEW_TOLERANCE:
            logger.warning(
                "input_runtime_recovery_activity_clock_skew future_seconds=%s",
                int((reference_at - now).total_seconds()),
            )
            return True
        age_seconds = max(0.0, (now - reference_at).total_seconds())
        return age_seconds > max_age

    def _session_auto_resume_expired(self, session_id: str, *, now: datetime) -> bool:
        return self._activity_expired(
            self._recovery_activity_at.get(session_id),
            now=now,
        )

    def _batch_auto_start_expired(self, batch, *, now: datetime) -> bool:
        return self._activity_expired(
            self._normalize_activity_timestamp(getattr(batch, "committed_at", None)),
            now=now,
        )

    async def _recover_committed_inputs(
        self,
        report,
    ) -> dict[str, InputAdmissionOutcome]:
        batches = getattr(self, "_recovery_committed_batches", None)
        if batches is None:
            batches = await self.committed_batches.list_committed_for_recovery()
        now = self._now()
        start_outcomes: dict[str, InputAdmissionOutcome] = {}
        per_session_last_sequence: dict[str, int] = {}
        blocked_sessions: set[str] = set()
        deferred: list[tuple[str, str]] = []

        for batch in batches:
            session_id = str(batch.session_id)
            input_batch_id = str(batch.input_batch_id)
            sequence = int(getattr(batch, "sequence_number", 0))
            previous = per_session_last_sequence.get(session_id, 0)
            if sequence <= previous:
                raise self._fatal("committed_batch_order_conflict")
            per_session_last_sequence[session_id] = sequence

            existing = (
                await self.repositories.admissions.get_by_input_batch_id(
                    input_batch_id
                )
            )
            if existing is None:
                state = await self.repositories.sessions.get(session_id)
                would_start_new_cycle = (
                    state is None or state.cycle_status in _TERMINAL_OR_IDLE
                )
                if (
                    would_start_new_cycle
                    and self._batch_auto_start_expired(batch, now=now)
                ):
                    logger.warning(
                        "input_runtime_recovery_committed_expired "
                        "session_id=%s input_batch_id=%s",
                        session_id,
                        input_batch_id,
                    )
                    continue

            if session_id in blocked_sessions and existing is None:
                deferred.append((session_id, input_batch_id))
                continue

            outcome = await self.admission_service.reconcile_committed_batch(
                input_batch_id,
                session_id=session_id,
            )
            if outcome.action == InputAdmissionAction.CAPACITY_BLOCKED:
                blocked_sessions.add(session_id)
                deferred.append((session_id, input_batch_id))
                continue

            if existing is None:
                report.committed_unadmitted_admitted += 1
            else:
                report.admissions_repaired += 1
            if (
                outcome.admission is not None
                and outcome.admission.cycle_sequence == 0
                and outcome.admission.state == AdmissionState.ADMITTED
                and outcome.should_start_runner
            ):
                start_outcomes[outcome.target_cycle_id or ""] = outcome

        installer = getattr(
            self.admission_service,
            "install_recovery_deferred",
            None,
        )
        if deferred:
            if not callable(installer):
                raise self._fatal("recovery_deferred_backlog_unsupported")
            installer(deferred)
            logger.warning(
                "input_runtime_recovery_capacity_deferred "
                "batches=%s sessions=%s",
                len(deferred),
                len({session_id for session_id, _ in deferred}),
            )
        elif callable(installer):
            installer(())

        return start_outcomes

    async def _build_session_plans(
        self,
        start_outcomes: dict[str, InputAdmissionOutcome],
        now: datetime,
        report,
    ) -> tuple[RecoverySessionPlan, ...]:
        plans = await super()._build_session_plans(start_outcomes, now, report)
        resolved: list[RecoverySessionPlan] = []

        for plan in plans:
            if (
                not plan.should_auto_schedule
                or not self._session_auto_resume_expired(
                    plan.session_id,
                    now=now,
                )
            ):
                resolved.append(plan)
                continue

            if plan.disposition == RecoveryDisposition.AUTO_RESUME_SAFE:
                snapshot = await self.repositories.snapshots.mark_recovery_interrupted(  # type: ignore[attr-defined]
                    session_id=plan.session_id,
                    cycle_id=plan.cycle_id,
                    generation=plan.generation,
                    reason_code=_RECOVERY_EXPIRY_REASON,
                    interrupted_at=now,
                )
                resolved.append(
                    RecoverySessionPlan(
                        session_id=plan.session_id,
                        cycle_id=plan.cycle_id,
                        generation=plan.generation,
                        disposition=RecoveryDisposition.INTERRUPTED,
                        snapshot=snapshot,
                        reason_code=_RECOVERY_EXPIRY_REASON,
                    )
                )
                logger.warning(
                    "input_runtime_recovery_auto_resume_expired "
                    "session_id=%s cycle_id=%s disposition=%s",
                    plan.session_id,
                    plan.cycle_id,
                    plan.disposition.value,
                )
                continue

            if plan.disposition == RecoveryDisposition.START_ADMITTED:
                await self.admission_service.control_service.request_reset(
                    session_id=plan.session_id,
                    idempotency_key=(
                        f"recovery-expiry:{plan.cycle_id}:{plan.generation}"
                    ),
                    source_client_type="runtime_recovery",
                    reason=_RECOVERY_EXPIRY_REASON,
                )
                clear_deferred = getattr(
                    self.admission_service,
                    "clear_recovery_deferred_session",
                    None,
                )
                if callable(clear_deferred):
                    clear_deferred(plan.session_id)
                report.resumable_cycles = max(0, report.resumable_cycles - 1)
                logger.warning(
                    "input_runtime_recovery_start_expired "
                    "session_id=%s cycle_id=%s",
                    plan.session_id,
                    plan.cycle_id,
                )
                continue

            resolved.append(plan)

        return tuple(resolved)
