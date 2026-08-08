"""IR-7 application reclassification for admission-vs-terminal races."""

from __future__ import annotations

from .admission import InputAdmissionAction, InputAdmissionOutcome
from .errors import (
    InputAdmissionDecisionStaleError,
    InputRuntimeConflictError,
)
from .ir4_admission import InputAdmissionService as _IR7InputAdmissionService
from .models import AdmissionKind, InputAdmissionRecord


_MAX_STALE_DECISION_RECLASSIFICATIONS = 1


class InputAdmissionService(_IR7InputAdmissionService):
    """Resolve one recognized stale admission classification in-process.

    The optimistic application read is not the admission linearization point.
    Filesystem/PostgreSQL-style repository coordination owns that ordering. If a
    previously active-cycle candidate reaches repository coordination after the
    session became terminal, the repository raises the dedicated stale-decision
    conflict before writes. This boundary then recomputes the complete admission
    decision for the same committed batch while retaining the admission-session
    application serialization boundary.
    """

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

        batch = await self._load_authoritative_batch(
            input_batch_id,
            session_id=session_id,
        )
        payload_size_bytes = int(self.payload_size_resolver(batch))
        if payload_size_bytes < 0:
            raise InputRuntimeConflictError(
                "committed batch payload size is invalid"
            )

        stale_reclassifications = 0
        async with self._hold_admission(session_id):
            while True:
                now = self._now()
                existing = (
                    await self.repositories.admissions.get_by_input_batch_id(
                        input_batch_id
                    )
                )
                if existing is not None:
                    return await self._duplicate_outcome(
                        existing,
                        requested_session_id=session_id,
                        now=now,
                    )

                state = await self._get_or_create_state(session_id, now=now)
                admission_kind, action, projection_key = self._decision(state)

                if admission_kind != AdmissionKind.START_CYCLE:
                    capacity_reason = await self._capacity_reason(
                        state=state,
                        payload_size_bytes=payload_size_bytes,
                    )
                    if capacity_reason is not None:
                        return InputAdmissionOutcome(
                            input_batch_id=input_batch_id,
                            session_id=session_id,
                            action=InputAdmissionAction.CAPACITY_BLOCKED,
                            should_start_runner=False,
                            should_wake_runner=False,
                            user_projection_key=(
                                "input_runtime.admission.capacity_blocked"
                            ),
                            retryable=True,
                            reason_code=capacity_reason,
                        )

                target_cycle_id = (
                    self.cycle_id_factory()
                    if admission_kind == AdmissionKind.START_CYCLE
                    else state.active_cycle_id
                )
                if not target_cycle_id:
                    raise InputRuntimeConflictError(
                        "active cycle identity is unavailable"
                    )

                candidate = InputAdmissionRecord(
                    session_id=session_id,
                    input_batch_id=input_batch_id,
                    session_sequence=1,
                    target_cycle_id=target_cycle_id,
                    cycle_sequence=(
                        0
                        if admission_kind == AdmissionKind.START_CYCLE
                        else 1
                    ),
                    admitted_generation=state.generation,
                    payload_size_bytes=payload_size_bytes,
                    admission_kind=admission_kind,
                    idempotency_key=f"committed-input:{input_batch_id}",
                    admitted_at=now,
                )

                try:
                    admission = await self.repositories.admissions.allocate(
                        candidate
                    )
                except InputAdmissionDecisionStaleError:
                    if (
                        stale_reclassifications
                        >= _MAX_STALE_DECISION_RECLASSIFICATIONS
                    ):
                        raise
                    stale_reclassifications += 1
                    # No durable relation for this batch exists at this point.
                    # Re-read authoritative state and recompute kind/action,
                    # target cycle, capacity and runner/wake flags from scratch.
                    continue

                if admission.session_id != session_id:
                    raise InputRuntimeConflictError(
                        "allocated admission belongs to another session"
                    )
                if admission.input_batch_id != input_batch_id:
                    raise InputRuntimeConflictError(
                        "allocated admission input identity mismatch"
                    )

                await self._ensure_inbox(admission, now=now)
                should_start = (
                    admission.admission_kind == AdmissionKind.START_CYCLE
                )
                should_wake = admission.admission_kind not in {
                    AdmissionKind.START_CYCLE,
                    AdmissionKind.QUEUE_PAUSED,
                }
                if should_wake:
                    await self._wake_best_effort(admission)

                return InputAdmissionOutcome.accepted(
                    admission=admission,
                    action=action,
                    should_start_runner=should_start,
                    should_wake_runner=should_wake,
                    user_projection_key=projection_key,
                    reason_code="admitted",
                )
