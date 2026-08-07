"""IR-4/IR-6 admission facade and runtime compatibility boundary."""

from __future__ import annotations

from dataclasses import dataclass

from .composition import register_input_runtime_binding
from .emissions import (
    AgentEmissionOutboxService,
    AgentEmissionService,
    ReplyAwareCommittedBatchReader,
)
from .errors import InputRuntimeConflictError
from .hardened_service import InputAdmissionService as _IR3InputAdmissionService
from .handoff import RuntimeHandoffState
from .ir4_persistence_windows import DurableClaimCycleInputApplier
from .ir5_hardening import (
    HardenedControlAwareCheckpointService,
    HardenedInputRuntimeControlService,
)
from .models import AdmissionKind, AdmissionState, CheckpointAction, CycleStatus


@dataclass(frozen=True, slots=True)
class DeferredWaitingApply:
    admission_id: str
    input_batch_id: str
    session_id: str
    cycle_id: str
    generation: int


class InputAdmissionService(_IR3InputAdmissionService):
    """Expose FIFO, controls and durable semantic emissions as one boundary."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.emission_service = AgentEmissionService(
            config=self.config,
            repository=self.repositories.emissions,
            committed_batches=self.committed_batches,
            clock=self.clock,
        )
        self.emission_outbox_service = AgentEmissionOutboxService(
            self.repositories.emissions,
            clock=self.clock,
            claim_lease_seconds=self.config.claim_lease_seconds,
        )
        self.reply_aware_committed_batches = ReplyAwareCommittedBatchReader(
            self.committed_batches,
            self.repositories.emissions,
        )
        self.cycle_input_applier = DurableClaimCycleInputApplier(
            config=self.config,
            repositories=self.repositories,
            committed_batches=self.reply_aware_committed_batches,
            clock=self.clock,
        )
        self.control_service = HardenedInputRuntimeControlService(
            repositories=self.repositories,
            wake_coordinator=self.wake_coordinator,
            clock=self.clock,
        )
        self.checkpoint_service = HardenedControlAwareCheckpointService(
            applier=self.cycle_input_applier,
            control_service=self.control_service,
        )
        self.application_binding = register_input_runtime_binding(
            config=self.config,
            repositories=self.repositories,
            committed_batches=self.committed_batches,
            checkpoint_service=self.checkpoint_service,
            emission_service=self.emission_service,
            emission_outbox_service=self.emission_outbox_service,
        )

    async def mark_initial_batch_applied(self, admission):
        """Never resurrect an old-generation initial admission after reset."""
        state = await self.repositories.sessions.get(admission.session_id)
        current = await self.repositories.admissions.get_by_input_batch_id(
            admission.input_batch_id
        )
        if current is None:
            raise InputRuntimeConflictError("admission disappeared")
        if (
            state is None
            or state.generation != admission.admitted_generation
            or (
                state.active_cycle_id is not None
                and state.active_cycle_id != admission.target_cycle_id
            )
        ):
            return current
        return await super().mark_initial_batch_applied(admission)

    async def record_cycle_status(
        self,
        *,
        session_id: str,
        cycle_id: str,
        status: CycleStatus,
    ):
        """Compatibility result mapping may not overwrite IR-5 authority.

        A paused cycle deliberately unwinds through an AgentResult shape which
        predates a PAUSED status. Reset can also leave an old runner returning
        after the durable generation has advanced. In both cases durable input
        runtime state wins over the compatibility result.
        """
        state = await self.repositories.sessions.get(session_id)
        if state is None:
            raise InputRuntimeConflictError("session state disappeared")
        if state.active_cycle_id != cycle_id:
            return state
        if state.cycle_status in {
            CycleStatus.PAUSE_REQUESTED,
            CycleStatus.PAUSED_BY_USER,
        }:
            return state
        return await super().record_cycle_status(
            session_id=session_id,
            cycle_id=cycle_id,
            status=status,
        )

    async def complete_runtime_handoff(
        self,
        admission,
        *,
        handoff_token: str,
    ):
        """Complete handoff first, then synchronize terminal snapshot evidence."""

        marker = await super().complete_runtime_handoff(
            admission,
            handoff_token=handoff_token,
        )
        if marker.state != RuntimeHandoffState.COMPLETED:
            return marker

        state = await self.repositories.sessions.get(admission.session_id)
        if (
            state is not None
            and state.active_cycle_id == admission.target_cycle_id
            and state.generation == admission.admitted_generation
            and state.cycle_status in {CycleStatus.DONE, CycleStatus.ERROR}
        ):
            outcome = await self.checkpoint_service.sync_terminal_snapshot(
                session_id=admission.session_id,
                cycle_id=admission.target_cycle_id,
                generation=admission.admitted_generation,
                status=state.cycle_status,
            )
            if outcome.action == CheckpointAction.INTERRUPT:
                raise InputRuntimeConflictError(
                    outcome.reason_code
                    or "terminal snapshot synchronization failed"
                )
        return marker

    async def mark_runtime_handoff_ambiguous(
        self,
        admission,
        *,
        handoff_token: str,
        error_code: str,
    ):
        """Persist ambiguity and retain a bounded applying-range evidence.

        This is recovery evidence only: it never installs context or marks any
        input applied. A later checkpoint sees the ambiguous marker and stops
        instead of blindly replaying the range.
        """
        marker = await super().mark_runtime_handoff_ambiguous(
            admission,
            handoff_token=handoff_token,
            error_code=error_code,
        )
        if admission.admission_kind != AdmissionKind.RESUME_WAITING:
            return marker
        state = await self.repositories.sessions.get(admission.session_id)
        if (
            state is None
            or state.active_cycle_id != admission.target_cycle_id
            or state.generation != admission.admitted_generation
        ):
            return marker
        claim = await self.repositories.inbox.claim_contiguous_range(
            admission.target_cycle_id,
            generation=admission.admitted_generation,
            after_sequence=state.active_cycle_applied_through_sequence,
            max_items=self.config.max_batches_per_checkpoint,
            max_bytes=self.config.max_batch_bytes_per_checkpoint,
            lease_seconds=self.config.claim_lease_seconds,
        )
        if claim is not None:
            await self.repositories.inbox.mark_applying(claim)
        return marker

    async def begin_waiting_compatibility_apply(self, admission):
        """Defer semantic ordering and mutation to the common CP-RESUME path."""
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
        """Require evidence that CP-RESUME applied the reply through FIFO."""
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
        """No compatibility claim exists; durable inbox state is unchanged."""
        return None
