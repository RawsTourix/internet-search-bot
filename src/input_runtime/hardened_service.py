"""IR-3 hardening for capacity authority and runtime handoff fencing."""

from __future__ import annotations

from datetime import datetime

from .admission import InputAdmissionAction, InputAdmissionOutcome
from .errors import InputRuntimeConflictError
from .handoff import RuntimeHandoffRecord, RuntimeHandoffState
from .handoff_context import (
    activate_runtime_handoff_context,
    clear_runtime_handoff_context_if_matches,
)
from .models import (
    AdmissionKind,
    AdmissionState,
    InboxState,
    InputAdmissionRecord,
)
from .service import InputAdmissionService as _BaseInputAdmissionService


_CAPACITY_STATES = {AdmissionState.ADMITTED}
_TERMINAL_INBOX_STATES = {
    InboxState.APPLIED,
    InboxState.CANCELLED,
    InboxState.FAILED_TERMINAL,
}


class InputAdmissionService(_BaseInputAdmissionService):
    """Use admissions as capacity authority and fence side-effecting handoff."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.runtime_handoffs = self.repositories.handoffs

    async def _capacity_reason(
        self,
        *,
        state,
        payload_size_bytes: int,
    ) -> str | None:
        if state.active_cycle_id is None:
            return None
        admissions = await self.repositories.admissions.list_for_session(
            state.session_id
        )
        inbox_items = await self.repositories.inbox.list_for_cycle(
            state.active_cycle_id
        )
        terminal_inbox_admissions = {
            item.admission_id
            for item in inbox_items
            if item.state in _TERMINAL_INBOX_STATES
        }
        reserved = []
        for item in admissions:
            if not (
                item.target_cycle_id == state.active_cycle_id
                and item.admitted_generation == state.generation
                and item.cycle_sequence > 0
                and item.cycle_sequence
                > state.active_cycle_applied_through_sequence
                and item.state in _CAPACITY_STATES
                and item.admission_id not in terminal_inbox_admissions
            ):
                continue
            marker = await self.runtime_handoffs.get(item.admission_id)
            if marker is not None and marker.state == RuntimeHandoffState.AMBIGUOUS:
                continue
            reserved.append(item)
        if len(reserved) >= self.config.max_queued_batches_per_session:
            return "max_queued_batches_per_session"
        queued_bytes = sum(item.payload_size_bytes for item in reserved)
        if (
            queued_bytes + payload_size_bytes
            > self.config.max_queued_bytes_per_session
        ):
            return "max_queued_bytes_per_session"
        return None

    async def _duplicate_outcome(
        self,
        admission: InputAdmissionRecord,
        *,
        requested_session_id: str,
        now: datetime,
    ) -> InputAdmissionOutcome:
        if admission.session_id != requested_session_id:
            raise InputRuntimeConflictError(
                "input batch is already admitted to another session"
            )
        await self._ensure_inbox(admission, now=now)
        marker = await self.runtime_handoffs.get(admission.admission_id)
        should_start, should_wake = self._flags(
            admission,
            InputAdmissionAction.DUPLICATE,
        )
        if marker is not None:
            should_start = False
            should_wake = False
        elif admission.admission_kind in {
            AdmissionKind.CONTINUE_RUNNING,
            AdmissionKind.RESUME_WAITING,
            AdmissionKind.RESUME_INTERRUPTED,
        } and admission.state == AdmissionState.ADMITTED:
            should_wake = True
        if should_wake:
            await self._wake_best_effort(admission)
        return InputAdmissionOutcome.accepted(
            admission=admission,
            action=InputAdmissionAction.DUPLICATE,
            should_start_runner=should_start,
            should_wake_runner=should_wake,
            user_projection_key="input_runtime.admission.duplicate",
            reason_code=(
                "runtime_handoff_already_recorded"
                if marker is not None
                else "existing_admission"
            ),
        )

    async def begin_runtime_handoff(
        self,
        admission: InputAdmissionRecord,
        *,
        handoff_token: str,
    ) -> bool:
        marker = RuntimeHandoffRecord(
            admission_id=admission.admission_id,
            session_id=admission.session_id,
            input_batch_id=admission.input_batch_id,
            cycle_id=admission.target_cycle_id,
            handoff_token=handoff_token,
            handed_off_at=self._now(),
        )
        current = await self.runtime_handoffs.begin(marker)
        owns_handoff = (
            current.handoff_token == handoff_token
            and current.state == RuntimeHandoffState.HANDED_OFF
        )
        if owns_handoff:
            activate_runtime_handoff_context(
                admission_id=admission.admission_id,
                session_id=admission.session_id,
                cycle_id=admission.target_cycle_id,
                generation=admission.admitted_generation,
                handoff_token=handoff_token,
            )
        return owns_handoff

    async def complete_runtime_handoff(
        self,
        admission: InputAdmissionRecord,
        *,
        handoff_token: str,
    ) -> RuntimeHandoffRecord:
        marker = await self.runtime_handoffs.complete(
            admission.admission_id,
            handoff_token=handoff_token,
            completed_at=self._now(),
        )
        clear_runtime_handoff_context_if_matches(
            admission_id=admission.admission_id,
            handoff_token=handoff_token,
        )
        return marker

    async def mark_runtime_handoff_ambiguous(
        self,
        admission: InputAdmissionRecord,
        *,
        handoff_token: str,
        error_code: str,
    ) -> RuntimeHandoffRecord | None:
        marker = await self.runtime_handoffs.get(admission.admission_id)
        if marker is None or marker.handoff_token != handoff_token:
            clear_runtime_handoff_context_if_matches(
                admission_id=admission.admission_id,
                handoff_token=handoff_token,
            )
            return None
        try:
            return await self.runtime_handoffs.mark_ambiguous(
                admission.admission_id,
                handoff_token=handoff_token,
                ambiguous_at=self._now(),
                error_code=error_code,
            )
        finally:
            clear_runtime_handoff_context_if_matches(
                admission_id=admission.admission_id,
                handoff_token=handoff_token,
            )

    async def get_runtime_handoff(
        self,
        admission: InputAdmissionRecord,
    ) -> RuntimeHandoffRecord | None:
        return await self.runtime_handoffs.get(admission.admission_id)

    async def requeue_waiting_compatibility_apply(
        self,
        claim,
        *,
        error_code: str,
    ) -> None:
        admission = await self.repositories.admissions.get_by_input_batch_id(
            claim.items[0].input_batch_id
        )
        if admission is None:
            raise InputRuntimeConflictError(
                "waiting compatibility admission disappeared"
            )
        marker = await self.runtime_handoffs.get(admission.admission_id)
        if marker is not None:
            # Once runtime invocation was durably handed off, requeue would be a
            # blind replay of potentially completed LLM/tool side effects.
            return
        await super().requeue_waiting_compatibility_apply(
            claim,
            error_code=error_code,
        )
