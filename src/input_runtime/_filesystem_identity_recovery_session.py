"""Crash-recoverable admission and control filesystem repositories."""

from __future__ import annotations

from typing import Callable, Iterable

from . import _filesystem_identity as identity_module
from ._filesystem_common import validated_copy
from ._filesystem_admission import (
    FileSystemInputAdmissionRepository as _AdmissionIdentityBase,
)
from ._filesystem_identity import (
    FileSystemSessionControlRepository as _ControlIdentityBase,
)
from ._filesystem_identity_recovery_common import (
    recover_cycle_authority,
    recover_indexed,
    scan_models,
)
from ._filesystem_session import (
    TERMINAL_OR_IDLE,
    _same_admission_relation,
    _same_control_relation,
)
from .errors import InputRuntimeConflictError, InputRuntimeNotFoundError
from .models import (
    AdmissionKind,
    ControlState,
    CycleStatus,
    InputAdmissionRecord,
    SessionControlCommand,
    SessionInputRuntimeState,
)
from .serialization import read_model


def atomic_write_model(path, model):
    """Keep the existing identity-module write seam used by crash tests."""
    return identity_module.atomic_write_model(path, model)


class FileSystemInputAdmissionRepository(_AdmissionIdentityBase):
    """Admission writes with record-first indexes and exact recovery scans."""

    def _scan_all(self) -> tuple[InputAdmissionRecord, ...]:
        return scan_models(
            self.layout.root.glob("sessions/*/admissions/*.json"),
            InputAdmissionRecord,
            identity_name="admission",
        )

    def _restore_indexes(self, record: InputAdmissionRecord) -> None:
        recover_cycle_authority(
            self,
            record.target_cycle_id,
            record.session_id,
        )
        self._index_record(record)

    def _recover_by_input(
        self,
        input_batch_id: str,
        scan: Callable[[], Iterable[InputAdmissionRecord]],
    ) -> InputAdmissionRecord | None:
        return recover_indexed(
            self,
            self.layout.admission_input(input_batch_id),
            InputAdmissionRecord,
            identity_name="input batch admission",
            matches_identity=lambda item: item.input_batch_id == input_batch_id,
            scan=scan,
            restore=self._restore_indexes,
        )

    def _recover_by_id(
        self,
        admission_id: str,
        scan: Callable[[], Iterable[InputAdmissionRecord]],
    ) -> InputAdmissionRecord | None:
        return recover_indexed(
            self,
            self.layout.record_index("admission", admission_id),
            InputAdmissionRecord,
            identity_name="admission",
            matches_identity=lambda item: item.admission_id == admission_id,
            scan=scan,
            restore=self._restore_indexes,
        )

    async def create_if_absent(
        self,
        record: InputAdmissionRecord,
    ) -> InputAdmissionRecord:
        async with self.locks.hold_identity_then_session(
            self.root,
            record.session_id,
        ):
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

            by_id = self._recover_by_id(record.admission_id, scan)
            if by_id is not None:
                if by_id != record:
                    raise InputRuntimeConflictError(
                        "admission stable ID collision"
                    )
                self._restore_indexes(by_id)
                return by_id

            rows = await self.list_for_session(record.session_id)
            if any(
                item.session_sequence == record.session_sequence
                for item in rows
            ):
                raise InputRuntimeConflictError(
                    "duplicate session admission sequence"
                )
            if any(
                item.target_cycle_id == record.target_cycle_id
                and item.cycle_sequence == record.cycle_sequence
                for item in rows
            ):
                raise InputRuntimeConflictError(
                    "duplicate cycle admission sequence"
                )

            recover_cycle_authority(
                self,
                record.target_cycle_id,
                record.session_id,
            )
            atomic_write_model(
                self.layout.admission(
                    record.session_id,
                    record.admission_id,
                ),
                record,
            )
            self._index_record(record)
            return record

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
            state = await self._repair_from_authoritative_admissions(
                state_path,
                state,
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
            atomic_write_model(
                self.layout.admission(
                    allocated.session_id,
                    allocated.admission_id,
                ),
                allocated,
            )
            self._index_record(allocated)
            atomic_write_model(state_path, next_state)
            return allocated


class FileSystemSessionControlRepository(_ControlIdentityBase):
    """Control writes with recoverable identity and atomic session sequencing."""

    def _scan_all(self) -> tuple[SessionControlCommand, ...]:
        return scan_models(
            self.layout.root.glob("sessions/*/controls/*.json"),
            SessionControlCommand,
            identity_name="control",
        )

    def _restore_indexes(self, command: SessionControlCommand) -> None:
        if command.target_cycle_id is not None:
            recover_cycle_authority(
                self,
                command.target_cycle_id,
                command.session_id,
            )
        self._index(command)
        if command.target_cycle_id is not None:
            self._ensure_cycle_authority(
                command.target_cycle_id,
                command.session_id,
            )

    def _recover_by_id(self, control_id: str) -> SessionControlCommand | None:
        return recover_indexed(
            self,
            self.layout.record_index("control", control_id),
            SessionControlCommand,
            identity_name="control",
            matches_identity=lambda item: item.control_id == control_id,
            scan=self._scan_all,
            restore=self._restore_indexes,
        )

    def _state_after_control(
        self,
        state: SessionInputRuntimeState,
        command: SessionControlCommand,
    ) -> SessionInputRuntimeState:
        return validated_copy(
            state,
            pending_control_sequence=max(
                state.pending_control_sequence,
                command.sequence_number,
            ),
            revision=state.revision + 1,
            updated_at=max(state.updated_at, command.created_at),
        )

    async def _repair_existing_pending(
        self,
        state_path,
        state: SessionInputRuntimeState,
        command: SessionControlCommand,
    ) -> SessionInputRuntimeState:
        if state.pending_control_sequence >= command.sequence_number:
            return state
        repaired = self._state_after_control(state, command)
        atomic_write_model(state_path, repaired)
        return repaired

    async def append(
        self,
        command: SessionControlCommand,
    ) -> SessionControlCommand:
        """Compatibility append; IR-5 acceptance uses allocate()."""
        async with self.locks.hold_identity_then_session(
            self.root,
            command.session_id,
        ):
            existing = await self.get_by_idempotency_key(
                command.session_id,
                command.idempotency_key,
            )
            if existing is not None:
                if not _same_control_relation(existing, command):
                    raise InputRuntimeConflictError(
                        "control idempotency relation changed"
                    )
                self._restore_indexes(existing)
                return existing

            by_id = self._recover_by_id(command.control_id)
            if by_id is not None:
                if by_id != command:
                    raise InputRuntimeConflictError(
                        "control stable ID collision"
                    )
                self._restore_indexes(by_id)
                return by_id

            if any(
                item.sequence_number == command.sequence_number
                for item in await self._all(command.session_id)
            ):
                raise InputRuntimeConflictError("duplicate control sequence")
            if command.target_cycle_id is not None:
                recover_cycle_authority(
                    self,
                    command.target_cycle_id,
                    command.session_id,
                )
            atomic_write_model(
                self.layout.control(
                    command.session_id,
                    command.control_id,
                ),
                command,
            )
            self._index(command)
            if command.target_cycle_id is not None:
                self._ensure_cycle_authority(
                    command.target_cycle_id,
                    command.session_id,
                )
            return command

    async def allocate(
        self,
        command: SessionControlCommand,
    ) -> SessionControlCommand:
        """Allocate one monotonic sequence and publish record before watermark."""
        async with self.locks.hold_identity_then_session(
            self.root,
            command.session_id,
        ):
            state_path = self.layout.state(command.session_id)
            if not state_path.exists():
                raise InputRuntimeNotFoundError(
                    "session runtime state required for control allocation"
                )
            state = read_model(state_path, SessionInputRuntimeState)
            existing = await self.get_by_idempotency_key(
                command.session_id,
                command.idempotency_key,
            )
            if existing is not None:
                if not _same_control_relation(existing, command):
                    raise InputRuntimeConflictError(
                        "control idempotency relation changed"
                    )
                self._restore_indexes(existing)
                await self._repair_existing_pending(state_path, state, existing)
                return existing
            if command.generation != state.generation:
                raise InputRuntimeConflictError("control generation changed")
            allocated = validated_copy(
                command,
                sequence_number=state.pending_control_sequence + 1,
            )
            by_id = self._recover_by_id(allocated.control_id)
            if by_id is not None:
                if by_id != allocated:
                    raise InputRuntimeConflictError(
                        "control stable ID collision"
                    )
                self._restore_indexes(by_id)
                await self._repair_existing_pending(state_path, state, by_id)
                return by_id
            if allocated.target_cycle_id is not None:
                recover_cycle_authority(
                    self,
                    allocated.target_cycle_id,
                    allocated.session_id,
                )
            atomic_write_model(
                self.layout.control(
                    allocated.session_id,
                    allocated.control_id,
                ),
                allocated,
            )
            self._index(allocated)
            atomic_write_model(
                state_path,
                self._state_after_control(state, allocated),
            )
            return allocated

    async def accept_reset(
        self,
        command: SessionControlCommand,
    ) -> tuple[SessionControlCommand, SessionInputRuntimeState]:
        """Publish reset then advance durable generation exactly once.

        The record-first order makes a failed state write repairable by the same
        idempotency key. Independent resets serialize on the repository session
        lock and therefore observe successive generations.
        """
        async with self.locks.hold_identity_then_session(
            self.root,
            command.session_id,
        ):
            state_path = self.layout.state(command.session_id)
            if not state_path.exists():
                raise InputRuntimeNotFoundError(
                    "session runtime state required for reset"
                )
            state = read_model(state_path, SessionInputRuntimeState)
            existing = await self.get_by_idempotency_key(
                command.session_id,
                command.idempotency_key,
            )
            if existing is not None:
                if not _same_control_relation(existing, command):
                    raise InputRuntimeConflictError(
                        "control idempotency relation changed"
                    )
                self._restore_indexes(existing)
                if state.generation == existing.generation:
                    next_state = validated_copy(
                        state,
                        generation=state.generation + 1,
                        active_cycle_id=None,
                        cycle_status=CycleStatus.IDLE,
                        active_cycle_accepted_through_sequence=0,
                        active_cycle_applied_through_sequence=0,
                        active_context_revision_id=None,
                        finalization_id=None,
                        pending_control_sequence=max(
                            state.pending_control_sequence,
                            existing.sequence_number,
                        ),
                        revision=state.revision + 1,
                        updated_at=max(state.updated_at, existing.created_at),
                    )
                    atomic_write_model(state_path, next_state)
                    return existing, next_state
                if state.generation >= existing.generation + 1:
                    repaired = await self._repair_existing_pending(
                        state_path,
                        state,
                        existing,
                    )
                    return existing, repaired
                raise InputRuntimeConflictError("reset generation diverged")

            if command.generation != state.generation:
                raise InputRuntimeConflictError("reset generation changed")
            allocated = validated_copy(
                command,
                sequence_number=state.pending_control_sequence + 1,
            )
            by_id = self._recover_by_id(allocated.control_id)
            if by_id is not None:
                if by_id != allocated:
                    raise InputRuntimeConflictError(
                        "control stable ID collision"
                    )
                self._restore_indexes(by_id)
                raise InputRuntimeConflictError(
                    "reset record exists without idempotency relation"
                )
            if allocated.target_cycle_id is not None:
                recover_cycle_authority(
                    self,
                    allocated.target_cycle_id,
                    allocated.session_id,
                )
            atomic_write_model(
                self.layout.control(
                    allocated.session_id,
                    allocated.control_id,
                ),
                allocated,
            )
            self._index(allocated)
            next_state = validated_copy(
                state,
                generation=state.generation + 1,
                active_cycle_id=None,
                cycle_status=CycleStatus.IDLE,
                active_cycle_accepted_through_sequence=0,
                active_cycle_applied_through_sequence=0,
                active_context_revision_id=None,
                finalization_id=None,
                pending_control_sequence=allocated.sequence_number,
                revision=state.revision + 1,
                updated_at=max(state.updated_at, allocated.created_at),
            )
            atomic_write_model(state_path, next_state)
            return allocated, next_state

    async def list_range(
        self,
        session_id: str,
        *,
        after_sequence: int,
        through_sequence: int,
    ) -> tuple[SessionControlCommand, ...]:
        if through_sequence <= after_sequence:
            return ()
        rows = [
            item
            for item in await self._all(session_id)
            if after_sequence < item.sequence_number <= through_sequence
        ]
        rows.sort(key=lambda item: item.sequence_number)
        return tuple(rows)

    async def cancel_generation_except(
        self,
        session_id: str,
        *,
        generation: int,
        reason_code: str,
        exclude_control_ids: tuple[str, ...] = (),
    ) -> tuple[SessionControlCommand, ...]:
        excluded = set(exclude_control_ids)
        changed: list[SessionControlCommand] = []
        async with self.locks.hold(self.root, session_id):
            for stale in await self._all(session_id):
                if stale.control_id in excluded:
                    continue
                current = read_model(
                    self.layout.control(session_id, stale.control_id),
                    SessionControlCommand,
                )
                if (
                    current.generation == generation
                    and current.state in {ControlState.QUEUED, ControlState.ACKNOWLEDGED}
                ):
                    updated = validated_copy(
                        current,
                        state=ControlState.CANCELLED,
                        acknowledged_at=None,
                        cancellation_reason_code=reason_code,
                    )
                    atomic_write_model(
                        self.layout.control(
                            session_id,
                            current.control_id,
                        ),
                        updated,
                    )
                    changed.append(updated)
        return tuple(changed)
