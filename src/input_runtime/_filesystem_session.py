"""Session-scoped filesystem repositories."""

from __future__ import annotations

from datetime import datetime

from ._filesystem_common import _RepositoryBase, validated_copy
from .errors import InputRuntimeConflictError, InputRuntimeNotFoundError
from .models import (
    AdmissionKind,
    AdmissionState,
    ControlState,
    CycleStatus,
    InputAdmissionRecord,
    SessionControlCommand,
    SessionInputRuntimeState,
)
from .serialization import atomic_write_model, list_models, read_model


TERMINAL_OR_IDLE = {
    CycleStatus.IDLE,
    CycleStatus.DONE,
    CycleStatus.ERROR,
    CycleStatus.CANCELLED,
}


def _same_admission_relation(
    existing: InputAdmissionRecord,
    incoming: InputAdmissionRecord,
) -> bool:
    return (
        existing.session_id,
        existing.input_batch_id,
        existing.target_cycle_id,
        existing.admission_kind,
        existing.idempotency_key,
        existing.admitted_generation,
        existing.payload_size_bytes,
    ) == (
        incoming.session_id,
        incoming.input_batch_id,
        incoming.target_cycle_id,
        incoming.admission_kind,
        incoming.idempotency_key,
        incoming.admitted_generation,
        incoming.payload_size_bytes,
    )


def _same_control_relation(
    existing: SessionControlCommand,
    incoming: SessionControlCommand,
) -> bool:
    return (
        existing.session_id,
        existing.target_cycle_id,
        existing.generation,
        existing.command,
        existing.idempotency_key,
        existing.source_client_type,
        existing.source_message_ref,
    ) == (
        incoming.session_id,
        incoming.target_cycle_id,
        incoming.generation,
        incoming.command,
        incoming.idempotency_key,
        incoming.source_client_type,
        incoming.source_message_ref,
    )


class FileSystemSessionInputRuntimeRepository(_RepositoryBase):
    async def create_if_absent(
        self,
        state: SessionInputRuntimeState,
    ) -> SessionInputRuntimeState:
        async with self.locks.hold(self.root, state.session_id):
            path = self.layout.state(state.session_id)
            if path.exists():
                current = read_model(path, SessionInputRuntimeState)
                if current != state:
                    raise InputRuntimeConflictError(
                        "session state already exists with different content"
                    )
                return current
            atomic_write_model(path, state)
            return state

    async def get(self, session_id: str) -> SessionInputRuntimeState | None:
        path = self.layout.state(session_id)
        return read_model(path, SessionInputRuntimeState) if path.exists() else None

    async def compare_and_swap(
        self,
        expected_revision: int,
        state: SessionInputRuntimeState,
    ) -> SessionInputRuntimeState:
        async with self.locks.hold(self.root, state.session_id):
            path = self.layout.state(state.session_id)
            if not path.exists():
                raise InputRuntimeNotFoundError(state.session_id)
            current = read_model(path, SessionInputRuntimeState)
            if current.revision != expected_revision:
                raise InputRuntimeConflictError("stale session state revision")
            if state.revision != expected_revision + 1:
                raise InputRuntimeConflictError(
                    "session state revision must advance by one"
                )
            atomic_write_model(path, state)
            return state

    async def list_states(self) -> tuple[SessionInputRuntimeState, ...]:
        directory = self.layout.root / "sessions"
        records = []
        if directory.exists():
            records = [
                read_model(path, SessionInputRuntimeState)
                for path in sorted(directory.glob("*/state.json"))
            ]
        return tuple(sorted(records, key=lambda item: item.session_id))


class FileSystemInputAdmissionRepository(_RepositoryBase):
    def _scan(self) -> tuple[InputAdmissionRecord, ...]:
        directory = self.layout.root / "sessions"
        if not directory.exists():
            return ()
        return tuple(
            read_model(path, InputAdmissionRecord)
            for path in sorted(directory.glob("*/admissions/*.json"))
        )

    def _index_record(self, record: InputAdmissionRecord) -> None:
        path = self.layout.admission(record.session_id, record.admission_id)
        pointer = self._pointer(
            "admission",
            record.admission_id,
            record.session_id,
            path,
            record.target_cycle_id,
        )
        self._write_pointer(
            self.layout.record_index("admission", record.admission_id),
            pointer,
        )
        self._write_pointer(
            self.layout.admission_input(record.input_batch_id),
            pointer,
        )
        self._ensure_cycle_authority(record.target_cycle_id, record.session_id)

    def _get_from_pointer(self, pointer) -> InputAdmissionRecord | None:
        if pointer is None:
            return None
        path = self._pointer_record_path(pointer)
        return read_model(path, InputAdmissionRecord) if path.exists() else None

    async def get_by_input_batch_id(
        self,
        input_batch_id: str,
    ) -> InputAdmissionRecord | None:
        pointer = self._read_pointer(
            self.layout.admission_input(input_batch_id)
        )
        record = self._get_from_pointer(pointer)
        if record is not None:
            if record.input_batch_id != input_batch_id:
                raise InputRuntimeConflictError(
                    "admission input index mismatch"
                )
            return record

        matches = [
            item for item in self._scan()
            if item.input_batch_id == input_batch_id
        ]
        if len(matches) > 1:
            raise InputRuntimeConflictError(
                "duplicate admissions for input batch"
            )
        if matches:
            self._index_record(matches[0])
            return matches[0]
        return None

    async def _find_id(self, admission_id: str) -> InputAdmissionRecord:
        record = self._get_from_pointer(
            self._read_pointer(
                self.layout.record_index("admission", admission_id)
            )
        )
        if record is not None:
            return record
        matches = [
            item for item in self._scan()
            if item.admission_id == admission_id
        ]
        if not matches:
            raise InputRuntimeNotFoundError(admission_id)
        if len(matches) > 1:
            raise InputRuntimeConflictError("duplicate admission stable ID")
        record = matches[0]
        self._index_record(record)
        return record

    async def create_if_absent(
        self,
        record: InputAdmissionRecord,
    ) -> InputAdmissionRecord:
        async with self.locks.hold(self.root, record.session_id):
            existing = await self.get_by_input_batch_id(record.input_batch_id)
            if existing is not None:
                if not _same_admission_relation(existing, record):
                    raise InputRuntimeConflictError(
                        "input batch admission relation changed"
                    )
                return existing

            try:
                by_id = await self._find_id(record.admission_id)
            except InputRuntimeNotFoundError:
                by_id = None
            if by_id is not None:
                if by_id != record:
                    raise InputRuntimeConflictError(
                        "admission stable ID collision"
                    )
                return by_id

            records = await self.list_for_session(record.session_id)
            if any(
                item.session_sequence == record.session_sequence
                for item in records
            ):
                raise InputRuntimeConflictError(
                    "duplicate session admission sequence"
                )
            if any(
                item.target_cycle_id == record.target_cycle_id
                and item.cycle_sequence == record.cycle_sequence
                for item in records
            ):
                raise InputRuntimeConflictError(
                    "duplicate cycle admission sequence"
                )

            self._ensure_cycle_authority(
                record.target_cycle_id,
                record.session_id,
            )
            atomic_write_model(
                self.layout.admission(record.session_id, record.admission_id),
                record,
            )
            self._index_record(record)
            return record

    def _state_after_admission(
        self,
        state: SessionInputRuntimeState,
        admission: InputAdmissionRecord,
    ) -> SessionInputRuntimeState:
        if state.generation != admission.admitted_generation:
            raise InputRuntimeConflictError("admission generation mismatch")

        if admission.admission_kind == AdmissionKind.START_CYCLE:
            if state.cycle_status not in TERMINAL_OR_IDLE:
                if state.active_cycle_id != admission.target_cycle_id:
                    raise InputRuntimeConflictError(
                        "session already has active cycle"
                    )
            return validated_copy(
                state,
                active_cycle_id=admission.target_cycle_id,
                cycle_status=CycleStatus.RUNNING,
                accepted_through_session_sequence=max(
                    state.accepted_through_session_sequence,
                    admission.session_sequence,
                ),
                active_cycle_accepted_through_sequence=0,
                active_cycle_applied_through_sequence=0,
                active_context_revision_id=None,
                finalization_id=None,
                revision=state.revision + 1,
                updated_at=admission.admitted_at,
            )

        if state.active_cycle_id != admission.target_cycle_id:
            raise InputRuntimeConflictError("target cycle is not active")
        return validated_copy(
            state,
            accepted_through_session_sequence=max(
                state.accepted_through_session_sequence,
                admission.session_sequence,
            ),
            active_cycle_accepted_through_sequence=max(
                state.active_cycle_accepted_through_sequence,
                admission.cycle_sequence,
            ),
            revision=state.revision + 1,
            updated_at=admission.admitted_at,
        )

    def _state_covers_admission(
        self,
        state: SessionInputRuntimeState,
        admission: InputAdmissionRecord,
    ) -> bool:
        if state.accepted_through_session_sequence < admission.session_sequence:
            return False
        if state.active_cycle_id != admission.target_cycle_id:
            return True
        return (
            state.active_cycle_accepted_through_sequence
            >= admission.cycle_sequence
        )

    def _repair_existing_allocation(
        self,
        state_path,
        state: SessionInputRuntimeState,
        admission: InputAdmissionRecord,
    ) -> SessionInputRuntimeState:
        if self._state_covers_admission(state, admission):
            return state
        repaired = self._state_after_admission(state, admission)
        atomic_write_model(state_path, repaired)
        return repaired

    async def allocate(
        self,
        record: InputAdmissionRecord,
    ) -> InputAdmissionRecord:
        async with self.locks.hold(self.root, record.session_id):
            state_path = self.layout.state(record.session_id)
            if not state_path.exists():
                raise InputRuntimeNotFoundError(
                    "session runtime state required for allocation"
                )
            state = read_model(state_path, SessionInputRuntimeState)

            existing = await self.get_by_input_batch_id(record.input_batch_id)
            if existing is not None:
                if not _same_admission_relation(existing, record):
                    raise InputRuntimeConflictError(
                        "input batch admission relation changed"
                    )
                self._repair_existing_allocation(
                    state_path,
                    state,
                    existing,
                )
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

            try:
                by_id = await self._find_id(allocated.admission_id)
            except InputRuntimeNotFoundError:
                by_id = None
            if by_id is not None and by_id != allocated:
                raise InputRuntimeConflictError(
                    "admission stable ID collision"
                )

            self._ensure_cycle_authority(
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

    async def _replace(
        self,
        admission_id: str,
        **updates: object,
    ) -> InputAdmissionRecord:
        stale = await self._find_id(admission_id)
        async with self.locks.hold(self.root, stale.session_id):
            current = await self._find_id(admission_id)
            updated = validated_copy(current, **updates)
            atomic_write_model(
                self.layout.admission(
                    current.session_id,
                    current.admission_id,
                ),
                updated,
            )
            return updated

    async def mark_applied(
        self,
        admission_id: str,
        *,
        applied_at: datetime,
    ) -> InputAdmissionRecord:
        return await self._replace(
            admission_id,
            state=AdmissionState.APPLIED,
            applied_at=applied_at,
        )

    async def cancel(
        self,
        admission_id: str,
        *,
        cancelled_at: datetime,
        reason_code: str,
    ) -> InputAdmissionRecord:
        return await self._replace(
            admission_id,
            state=AdmissionState.CANCELLED,
            cancelled_at=cancelled_at,
            cancellation_reason_code=reason_code,
        )

    async def list_for_session(
        self,
        session_id: str,
    ) -> tuple[InputAdmissionRecord, ...]:
        records = list_models(
            self.layout.admissions(session_id),
            InputAdmissionRecord,
        )
        return tuple(
            sorted(records, key=lambda item: item.session_sequence)
        )

    async def list_unapplied(
        self,
        session_id: str,
    ) -> tuple[InputAdmissionRecord, ...]:
        return tuple(
            item
            for item in await self.list_for_session(session_id)
            if item.state == AdmissionState.ADMITTED
        )

    async def cancel_generation(
        self,
        session_id: str,
        *,
        generation: int,
        cancelled_at: datetime,
        reason_code: str,
    ) -> tuple[InputAdmissionRecord, ...]:
        changed = []
        async with self.locks.hold(self.root, session_id):
            for stale in await self.list_for_session(session_id):
                current = read_model(
                    self.layout.admission(session_id, stale.admission_id),
                    InputAdmissionRecord,
                )
                if (
                    current.admitted_generation == generation
                    and current.state == AdmissionState.ADMITTED
                ):
                    updated = validated_copy(
                        current,
                        state=AdmissionState.CANCELLED,
                        cancelled_at=cancelled_at,
                        cancellation_reason_code=reason_code,
                    )
                    atomic_write_model(
                        self.layout.admission(
                            session_id,
                            current.admission_id,
                        ),
                        updated,
                    )
                    changed.append(updated)
        return tuple(changed)


class FileSystemSessionControlRepository(_RepositoryBase):
    async def _all(
        self,
        session_id: str,
    ) -> tuple[SessionControlCommand, ...]:
        records = list_models(
            self.layout.controls(session_id),
            SessionControlCommand,
        )
        return tuple(
            sorted(records, key=lambda item: item.sequence_number)
        )

    def _index(self, record: SessionControlCommand) -> None:
        pointer = self._pointer(
            "control",
            record.control_id,
            record.session_id,
            self.layout.control(record.session_id, record.control_id),
            record.target_cycle_id,
        )
        self._write_pointer(
            self.layout.record_index("control", record.control_id),
            pointer,
        )

    async def _find(self, control_id: str) -> SessionControlCommand:
        pointer = self._read_pointer(
            self.layout.record_index("control", control_id)
        )
        if pointer and self._pointer_record_path(pointer).exists():
            return read_model(
                self._pointer_record_path(pointer),
                SessionControlCommand,
            )

        directory = self.layout.root / "sessions"
        records = []
        if directory.exists():
            for path in sorted(directory.glob("*/controls/*.json")):
                record = read_model(path, SessionControlCommand)
                if record.control_id == control_id:
                    records.append(record)
        if not records:
            raise InputRuntimeNotFoundError(control_id)
        if len(records) > 1:
            raise InputRuntimeConflictError("duplicate control stable ID")
        self._index(records[0])
        return records[0]

    async def append(
        self,
        command: SessionControlCommand,
    ) -> SessionControlCommand:
        async with self.locks.hold(self.root, command.session_id):
            existing = await self.get_by_idempotency_key(
                command.session_id,
                command.idempotency_key,
            )
            if existing is not None:
                if not _same_control_relation(existing, command):
                    raise InputRuntimeConflictError(
                        "control idempotency relation changed"
                    )
                return existing

            try:
                by_id = await self._find(command.control_id)
            except InputRuntimeNotFoundError:
                by_id = None
            if by_id is not None and by_id != command:
                raise InputRuntimeConflictError(
                    "control stable ID collision"
                )
            if any(
                item.sequence_number == command.sequence_number
                for item in await self._all(command.session_id)
            ):
                raise InputRuntimeConflictError("duplicate control sequence")

            atomic_write_model(
                self.layout.control(command.session_id, command.control_id),
                command,
            )
            self._index(command)
            return command

    async def get_by_idempotency_key(
        self,
        session_id: str,
        idempotency_key: str,
    ) -> SessionControlCommand | None:
        return next(
            (
                item
                for item in await self._all(session_id)
                if item.idempotency_key == idempotency_key.strip()
            ),
            None,
        )

    async def _replace(
        self,
        control_id: str,
        **updates: object,
    ) -> SessionControlCommand:
        stale = await self._find(control_id)
        async with self.locks.hold(self.root, stale.session_id):
            current = await self._find(control_id)
            updated = validated_copy(current, **updates)
            atomic_write_model(
                self.layout.control(
                    current.session_id,
                    current.control_id,
                ),
                updated,
            )
            return updated

    async def acknowledge(
        self,
        control_id: str,
        *,
        acknowledged_at: datetime,
    ) -> SessionControlCommand:
        return await self._replace(
            control_id,
            state=ControlState.ACKNOWLEDGED,
            acknowledged_at=acknowledged_at,
        )

    async def apply(
        self,
        control_id: str,
        *,
        applied_at: datetime,
    ) -> SessionControlCommand:
        record = await self._find(control_id)
        return await self._replace(
            control_id,
            state=ControlState.APPLIED,
            acknowledged_at=record.acknowledged_at or applied_at,
            applied_at=applied_at,
        )

    async def reject(
        self,
        control_id: str,
        *,
        rejection_code: str,
    ) -> SessionControlCommand:
        return await self._replace(
            control_id,
            state=ControlState.REJECTED,
            rejection_code=rejection_code,
        )

    async def list_pending(
        self,
        session_id: str,
        *,
        generation: int,
    ) -> tuple[SessionControlCommand, ...]:
        return tuple(
            item
            for item in await self._all(session_id)
            if item.generation == generation
            and item.state in {
                ControlState.QUEUED,
                ControlState.ACKNOWLEDGED,
            }
        )

    async def cancel_generation(
        self,
        session_id: str,
        *,
        generation: int,
        reason_code: str,
    ) -> tuple[SessionControlCommand, ...]:
        changed = []
        async with self.locks.hold(self.root, session_id):
            for stale in await self._all(session_id):
                current = read_model(
                    self.layout.control(session_id, stale.control_id),
                    SessionControlCommand,
                )
                if (
                    current.generation == generation
                    and current.state
                    in {ControlState.QUEUED, ControlState.ACKNOWLEDGED}
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
