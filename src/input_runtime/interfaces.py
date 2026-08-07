"""Command-oriented repository ports for the input runtime."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from .handoff import RuntimeHandoffRecord
from .models import (
    ActiveCycleSnapshot,
    AgentEmission,
    ClaimedInboxRange,
    CycleContextRevision,
    CycleFinalizationRecord,
    CycleInboxItem,
    InputAdmissionRecord,
    SessionControlCommand,
    SessionInputRuntimeState,
)


@runtime_checkable
class SessionInputRuntimeRepository(Protocol):
    async def create_if_absent(
        self,
        state: SessionInputRuntimeState,
    ) -> SessionInputRuntimeState: ...

    async def get(
        self,
        session_id: str,
    ) -> SessionInputRuntimeState | None: ...

    async def compare_and_swap(
        self,
        expected_revision: int,
        state: SessionInputRuntimeState,
    ) -> SessionInputRuntimeState: ...

    async def list_states(self) -> tuple[SessionInputRuntimeState, ...]: ...


@runtime_checkable
class InputAdmissionRepository(Protocol):
    async def create_if_absent(
        self,
        record: InputAdmissionRecord,
    ) -> InputAdmissionRecord: ...

    async def get_by_input_batch_id(
        self,
        input_batch_id: str,
    ) -> InputAdmissionRecord | None: ...

    async def allocate(
        self,
        record: InputAdmissionRecord,
    ) -> InputAdmissionRecord: ...

    async def mark_applied(
        self,
        admission_id: str,
        *,
        applied_at: datetime,
    ) -> InputAdmissionRecord: ...

    async def cancel(
        self,
        admission_id: str,
        *,
        cancelled_at: datetime,
        reason_code: str,
    ) -> InputAdmissionRecord: ...

    async def list_for_session(
        self,
        session_id: str,
    ) -> tuple[InputAdmissionRecord, ...]: ...

    async def list_unapplied(
        self,
        session_id: str,
    ) -> tuple[InputAdmissionRecord, ...]: ...

    async def cancel_generation(
        self,
        session_id: str,
        *,
        generation: int,
        cancelled_at: datetime,
        reason_code: str,
    ) -> tuple[InputAdmissionRecord, ...]: ...


@runtime_checkable
class CycleInboxRepository(Protocol):
    async def create_if_absent(
        self,
        item: CycleInboxItem,
    ) -> CycleInboxItem: ...

    async def claim_contiguous_range(
        self,
        cycle_id: str,
        *,
        generation: int,
        after_sequence: int,
        max_items: int,
        max_bytes: int,
        lease_seconds: int,
    ) -> ClaimedInboxRange | None: ...

    async def mark_applying(
        self,
        claim: ClaimedInboxRange,
    ) -> ClaimedInboxRange: ...

    async def mark_applied(
        self,
        claim: ClaimedInboxRange,
        *,
        applied_at: datetime,
    ) -> tuple[CycleInboxItem, ...]: ...

    async def requeue_claim(
        self,
        claim: ClaimedInboxRange,
        *,
        error_code: str | None = None,
    ) -> tuple[CycleInboxItem, ...]: ...

    async def list_for_cycle(
        self,
        cycle_id: str,
    ) -> tuple[CycleInboxItem, ...]: ...

    async def recover_expired_claims(
        self,
        *,
        now: datetime,
    ) -> tuple[CycleInboxItem, ...]: ...

    async def cancel_generation(
        self,
        session_id: str,
        *,
        generation: int,
        cancelled_at: datetime,
        reason_code: str,
    ) -> tuple[CycleInboxItem, ...]: ...


@runtime_checkable
class RuntimeHandoffRepository(Protocol):
    async def get(
        self,
        admission_id: str,
    ) -> RuntimeHandoffRecord | None: ...

    async def begin(
        self,
        candidate: RuntimeHandoffRecord,
    ) -> RuntimeHandoffRecord: ...

    async def complete(
        self,
        admission_id: str,
        *,
        handoff_token: str,
        completed_at: datetime,
    ) -> RuntimeHandoffRecord: ...

    async def mark_ambiguous(
        self,
        admission_id: str,
        *,
        handoff_token: str,
        ambiguous_at: datetime,
        error_code: str,
    ) -> RuntimeHandoffRecord: ...


@runtime_checkable
class SessionControlRepository(Protocol):
    async def append(
        self,
        command: SessionControlCommand,
    ) -> SessionControlCommand: ...

    async def get_by_idempotency_key(
        self,
        session_id: str,
        idempotency_key: str,
    ) -> SessionControlCommand | None: ...

    async def accept_continue(
        self,
        command: SessionControlCommand,
    ) -> SessionControlCommand: ...

    async def acknowledge(
        self,
        control_id: str,
        *,
        acknowledged_at: datetime,
    ) -> SessionControlCommand: ...

    async def apply(
        self,
        control_id: str,
        *,
        applied_at: datetime,
    ) -> SessionControlCommand: ...

    async def reject(
        self,
        control_id: str,
        *,
        rejection_code: str,
    ) -> SessionControlCommand: ...

    async def list_pending(
        self,
        session_id: str,
        *,
        generation: int,
    ) -> tuple[SessionControlCommand, ...]: ...

    async def cancel_generation(
        self,
        session_id: str,
        *,
        generation: int,
        reason_code: str,
    ) -> tuple[SessionControlCommand, ...]: ...


@runtime_checkable
class ActiveCycleSnapshotRepository(Protocol):
    async def create_if_absent(
        self,
        snapshot: ActiveCycleSnapshot,
    ) -> ActiveCycleSnapshot: ...

    async def get(
        self,
        cycle_id: str,
    ) -> ActiveCycleSnapshot | None: ...

    async def compare_and_swap(
        self,
        expected_revision: int,
        snapshot: ActiveCycleSnapshot,
    ) -> ActiveCycleSnapshot: ...

    async def list_active(self) -> tuple[ActiveCycleSnapshot, ...]: ...

    async def list_resumable(self) -> tuple[ActiveCycleSnapshot, ...]: ...

    async def cancel_generation(
        self,
        session_id: str,
        *,
        generation: int,
        reason_code: str,
    ) -> tuple[ActiveCycleSnapshot, ...]: ...


@runtime_checkable
class ContextRevisionRepository(Protocol):
    async def append_revision(
        self,
        revision: CycleContextRevision,
    ) -> CycleContextRevision: ...

    async def get(
        self,
        context_revision_id: str,
    ) -> CycleContextRevision | None: ...

    async def get_latest(
        self,
        cycle_id: str,
    ) -> CycleContextRevision | None: ...

    async def list_for_cycle(
        self,
        cycle_id: str,
    ) -> tuple[CycleContextRevision, ...]: ...


@runtime_checkable
class AgentEmissionRepository(Protocol):
    async def create_if_absent(
        self,
        emission: AgentEmission,
    ) -> AgentEmission: ...

    async def get_by_idempotency_key(
        self,
        cycle_id: str,
        idempotency_key: str,
    ) -> AgentEmission | None: ...

    async def claim_delivery(
        self,
        emission_id: str,
        *,
        claim_token: str,
        claimed_at: datetime | None = None,
        lease_seconds: int = 300,
    ) -> AgentEmission: ...

    async def complete_delivery(
        self,
        emission_id: str,
        *,
        claim_token: str,
        delivered_at: datetime,
    ) -> AgentEmission: ...

    async def fail_delivery(
        self,
        emission_id: str,
        *,
        claim_token: str,
        state: str,
        error_code: str,
    ) -> AgentEmission: ...

    async def recover_expired_delivery_claims(
        self,
        *,
        now: datetime,
    ) -> tuple[AgentEmission, ...]: ...

    async def list_pending_delivery(self) -> tuple[AgentEmission, ...]: ...

    async def cancel_generation(
        self,
        session_id: str,
        *,
        generation: int,
        reason_code: str,
    ) -> tuple[AgentEmission, ...]: ...


@runtime_checkable
class FinalizationRepository(Protocol):
    async def prepare(
        self,
        record: CycleFinalizationRecord,
    ) -> CycleFinalizationRecord: ...

    async def get(
        self,
        finalization_id: str,
    ) -> CycleFinalizationRecord | None: ...

    async def advance(
        self,
        finalization_id: str,
        *,
        expected_state: str,
        next_record: CycleFinalizationRecord,
    ) -> CycleFinalizationRecord: ...

    async def abort(
        self,
        finalization_id: str,
        *,
        expected_state: str,
        next_record: CycleFinalizationRecord,
    ) -> CycleFinalizationRecord: ...

    async def list_recoverable(
        self,
    ) -> tuple[CycleFinalizationRecord, ...]: ...

    async def cancel_generation(
        self,
        session_id: str,
        *,
        generation: int,
        reason_code: str,
    ) -> tuple[CycleFinalizationRecord, ...]: ...
