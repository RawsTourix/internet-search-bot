"""IR-4 admission facade: explicit composition and common FIFO waiting apply."""

from __future__ import annotations

from dataclasses import dataclass

from .composition import register_input_runtime_binding
from .errors import InputRuntimeConflictError
from .hardened_service import InputAdmissionService as _IR3InputAdmissionService
from .models import AdmissionKind, AdmissionState


@dataclass(frozen=True, slots=True)
class DeferredWaitingApply:
    admission_id: str
    input_batch_id: str
    session_id: str
    cycle_id: str
    generation: int


class InputAdmissionService(_IR3InputAdmissionService):
    """Expose IR-4 services while preserving the admitted runtime boundary."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        register_input_runtime_binding(
            config=self.config,
            repositories=self.repositories,
            committed_batches=self.committed_batches,
        )

    async def begin_waiting_compatibility_apply(self, admission):
        """Defer semantic apply to CP-RESUME instead of claiming reply alone."""
        if admission.admission_kind != AdmissionKind.RESUME_WAITING:
            raise InputRuntimeConflictError(
                "only WAITING_USER admission may use compatibility resume"
            )
        if admission.state == AdmissionState.APPLIED:
            return None
        state = await self.repositories.sessions.get(admission.session_id)
        if (
            state is None
            or state.active_cycle_id != admission.target_cycle_id
            or state.generation != admission.admitted_generation
        ):
            raise InputRuntimeConflictError(
                "waiting resume lost active cycle authority"
            )
        return DeferredWaitingApply(
            admission_id=admission.admission_id,
            input_batch_id=admission.input_batch_id,
            session_id=admission.session_id,
            cycle_id=admission.target_cycle_id,
            generation=admission.admitted_generation,
        )

    async def complete_waiting_compatibility_apply(self, claim) -> None:
        """The common checkpoint applier owns all durable apply transitions."""
        if claim is None:
            return
        current = await self.repositories.admissions.get_by_input_batch_id(
            claim.input_batch_id
        )
        if current is None or current.state != AdmissionState.APPLIED:
            raise InputRuntimeConflictError(
                "waiting input was not applied by common FIFO checkpoint"
            )

    async def requeue_waiting_compatibility_apply(
        self,
        claim,
        *,
        error_code: str,
    ) -> None:
        """No claim exists before handoff; queued inbox remains authoritative."""
        return None
