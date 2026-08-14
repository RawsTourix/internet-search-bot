"""Capacity-aware startup recovery for committed durable input."""

from __future__ import annotations

import logging

from src.input_runtime.admission import InputAdmissionAction, InputAdmissionOutcome
from src.input_runtime.models import AdmissionState
from src.input_runtime.recovery_terminal import (
    InputRuntimeRecoveryCoordinator as _TerminalRecoveryCoordinator,
)


logger = logging.getLogger("InputRuntime.Recovery.Backpressure")


class InputRuntimeRecoveryCoordinator(_TerminalRecoveryCoordinator):
    """Defer bounded-capacity overflow without weakening recovery validation."""

    async def _recover_committed_inputs(
        self,
        report,
    ) -> dict[str, InputAdmissionOutcome]:
        batches = await self.committed_batches.list_committed_for_recovery()
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
