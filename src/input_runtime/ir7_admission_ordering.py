"""IR-7 durable admission ordering against terminal session authority."""

from __future__ import annotations

from . import _filesystem_identity_recovery_session as base_module
from ._filesystem_common import validated_copy
from ._filesystem_identity_recovery_common import recover_cycle_authority
from ._filesystem_session import TERMINAL_OR_IDLE, _same_admission_relation
from .errors import (
    InputAdmissionDecisionStaleError,
    InputRuntimeConflictError,
    InputRuntimeNotFoundError,
)
from .models import (
    AdmissionKind,
    CycleStatus,
    InputAdmissionRecord,
    SessionInputRuntimeState,
)
from .serialization import read_model


STALE_TERMINAL_ADMISSION_REASON = (
    "terminal_state_after_optimistic_admission_decision"
)
_PRE_REPAIR_TERMINAL = {
    CycleStatus.DONE,
    CycleStatus.ERROR,
    CycleStatus.CANCELLED,
}


class FileSystemInputAdmissionRepository(
    base_module.FileSystemInputAdmissionRepository
):
    """Reject stale active-cycle classification before any admission write.

    Application admission may classify a batch from an optimistic session read.
    The durable root/session coordination acquired here is the actual ordering
    point against terminal commit. If terminal authority won first, a non-start
    candidate is rejected as a dedicated, retryable classification condition
    before record/index/inbox/session-watermark mutation for this batch.

    A raw IDLE state is special: IR-2 record-first crash recovery may still have
    an authoritative START_CYCLE admission that must repair the state to RUNNING.
    Therefore DONE/ERROR/CANCELLED are fenced immediately, while IDLE is judged
    only after authoritative-admission repair. Corruption conflicts from that
    repair remain authoritative and are never reclassified/retried as this race.
    """

    @staticmethod
    def _reject_stale_terminal_decision(
        record: InputAdmissionRecord,
        state: SessionInputRuntimeState,
        *,
        include_idle: bool,
    ) -> None:
        terminal_states = (
            TERMINAL_OR_IDLE if include_idle else _PRE_REPAIR_TERMINAL
        )
        if (
            record.admission_kind != AdmissionKind.START_CYCLE
            and state.cycle_status in terminal_states
        ):
            raise InputAdmissionDecisionStaleError(
                STALE_TERMINAL_ADMISSION_REASON
            )

    async def allocate(
        self,
        record: InputAdmissionRecord,
    ) -> InputAdmissionRecord:
        async with self.locks.hold_identity_then_session(
            self.root,
            record.session_id,
        ):
            state_path = self.layout.state(record.session_id)
            if not state_path.exists():
                raise InputRuntimeNotFoundError(
                    "session runtime state required for allocation"
                )
            state = read_model(state_path, SessionInputRuntimeState)

            # A real terminal commit must fence the stale candidate before any
            # repair/write for this batch. IDLE is deferred because record-first
            # admission recovery may legitimately reconstruct an active cycle.
            self._reject_stale_terminal_decision(
                record,
                state,
                include_idle=False,
            )

            state = await self._repair_from_authoritative_admissions(
                state_path,
                state,
            )
            self._reject_stale_terminal_decision(
                record,
                state,
                include_idle=True,
            )

            cached_rows: tuple[InputAdmissionRecord, ...] | None = None

            def scan() -> tuple[InputAdmissionRecord, ...]:
                nonlocal cached_rows
                if cached_rows is None:
                    cached_rows = self._scan_all()
                return cached_rows

            existing = self._recover_by_input(record.input_batch_id, scan)
            if existing is not None:
                if not _same_admission_relation(existing, record):
                    raise InputRuntimeConflictError(
                        "input batch admission relation changed"
                    )
                self._restore_indexes(existing)
                return existing

            session_sequence = state.accepted_through_session_sequence + 1
            if record.admission_kind == AdmissionKind.START_CYCLE:
                if state.cycle_status not in TERMINAL_OR_IDLE:
                    raise InputRuntimeConflictError(
                        "session already has active cycle"
                    )
                cycle_sequence = 0
            else:
                if state.active_cycle_id != record.target_cycle_id:
                    raise InputRuntimeConflictError(
                        "target cycle is not active"
                    )
                cycle_sequence = (
                    state.active_cycle_accepted_through_sequence + 1
                )

            allocated = validated_copy(
                record,
                session_sequence=session_sequence,
                cycle_sequence=cycle_sequence,
            )
            next_state = self._state_after_admission(state, allocated)
            if next_state.updated_at < state.updated_at:
                next_state = validated_copy(
                    next_state,
                    updated_at=state.updated_at,
                )

            by_id = self._recover_by_id(allocated.admission_id, scan)
            if by_id is not None:
                if by_id != allocated:
                    raise InputRuntimeConflictError(
                        "admission stable ID collision"
                    )
                self._restore_indexes(by_id)
                return by_id

            recover_cycle_authority(
                self,
                allocated.target_cycle_id,
                allocated.session_id,
            )
            base_module.atomic_write_model(
                self.layout.admission(
                    allocated.session_id,
                    allocated.admission_id,
                ),
                allocated,
            )
            self._index_record(allocated)
            base_module.atomic_write_model(state_path, next_state)
            return allocated
