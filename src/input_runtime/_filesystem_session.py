"""Session-scoped filesystem repositories."""

from __future__ import annotations

from datetime import datetime

from .errors import InputRuntimeConflictError, InputRuntimeNotFoundError
from .models import (
    AdmissionState, ControlState, InputAdmissionRecord,
    SessionControlCommand, SessionInputRuntimeState,
)
from .serialization import atomic_write_model, list_models, read_model
from ._filesystem_common import _RepositoryBase, validated_copy


class FileSystemSessionInputRuntimeRepository(_RepositoryBase):
    async def create_if_absent(self, state: SessionInputRuntimeState) -> SessionInputRuntimeState:
        async with self.locks.hold(self.root, state.session_id):
            path = self.layout.state(state.session_id)
            if path.exists():
                return read_model(path, SessionInputRuntimeState)
            atomic_write_model(path, state)
            return state

    async def get(self, session_id: str) -> SessionInputRuntimeState | None:
        path = self.layout.state(session_id)
        return read_model(path, SessionInputRuntimeState) if path.exists() else None

    async def compare_and_swap(self, expected_revision: int, state: SessionInputRuntimeState) -> SessionInputRuntimeState:
        async with self.locks.hold(self.root, state.session_id):
            path = self.layout.state(state.session_id)
            if not path.exists():
                raise InputRuntimeNotFoundError(state.session_id)
            current = read_model(path, SessionInputRuntimeState)
            if current.revision != expected_revision:
                raise InputRuntimeConflictError("stale session state revision")
            if state.revision != expected_revision + 1:
                raise InputRuntimeConflictError("session state revision must advance by one")
            atomic_write_model(path, state)
            return state

    async def list_states(self) -> tuple[SessionInputRuntimeState, ...]:
        sessions = self.layout.root / "sessions"
        records = []
        if sessions.exists():
            for path in sorted(sessions.glob("*/state.json")):
                records.append(read_model(path, SessionInputRuntimeState))
        return tuple(sorted(records, key=lambda item: item.session_id))


class FileSystemInputAdmissionRepository(_RepositoryBase):
    async def _all(self) -> tuple[InputAdmissionRecord, ...]:
        sessions = self.layout.root / "sessions"
        records = []
        if sessions.exists():
            for path in sorted(sessions.glob("*/admissions/*.json")):
                records.append(read_model(path, InputAdmissionRecord))
        return tuple(records)

    async def create_if_absent(self, record: InputAdmissionRecord) -> InputAdmissionRecord:
        async with self.locks.hold(self.root, record.session_id):
            existing = await self.get_by_input_batch_id(record.input_batch_id)
            if existing is not None:
                return existing
            records = await self.list_for_session(record.session_id)
            if any(item.session_sequence == record.session_sequence for item in records):
                raise InputRuntimeConflictError("duplicate session admission sequence")
            if any(item.target_cycle_id == record.target_cycle_id and item.cycle_sequence == record.cycle_sequence for item in records):
                raise InputRuntimeConflictError("duplicate cycle admission sequence")
            atomic_write_model(self.layout.admission(record.session_id, record.admission_id), record)
            return record

    async def get_by_input_batch_id(self, input_batch_id: str) -> InputAdmissionRecord | None:
        for record in await self._all():
            if record.input_batch_id == input_batch_id:
                return record
        return None

    async def allocate(self, record: InputAdmissionRecord) -> InputAdmissionRecord:
        return await self.create_if_absent(record)

    async def _replace(self, admission_id: str, **updates: object) -> InputAdmissionRecord:
        for record in await self._all():
            if record.admission_id == admission_id:
                async with self.locks.hold(self.root, record.session_id):
                    path = self.layout.admission(record.session_id, admission_id)
                    current = read_model(path, InputAdmissionRecord)
                    updated = validated_copy(current, **updates)
                    atomic_write_model(path, updated)
                    return updated
        raise InputRuntimeNotFoundError(admission_id)

    async def mark_applied(self, admission_id: str, *, applied_at: datetime) -> InputAdmissionRecord:
        return await self._replace(admission_id, state=AdmissionState.APPLIED, applied_at=applied_at)

    async def cancel(self, admission_id: str, *, cancelled_at: datetime, reason_code: str) -> InputAdmissionRecord:
        return await self._replace(admission_id, state=AdmissionState.CANCELLED, cancelled_at=cancelled_at, failure_code=None)

    async def list_for_session(self, session_id: str) -> tuple[InputAdmissionRecord, ...]:
        records = list_models(self.layout.admissions(session_id), InputAdmissionRecord)
        return tuple(sorted(records, key=lambda item: item.session_sequence))

    async def list_unapplied(self, session_id: str) -> tuple[InputAdmissionRecord, ...]:
        return tuple(item for item in await self.list_for_session(session_id) if item.state == AdmissionState.ADMITTED)

    async def cancel_generation(self, session_id: str, *, generation: int, cancelled_at: datetime, reason_code: str) -> tuple[InputAdmissionRecord, ...]:
        changed = []
        async with self.locks.hold(self.root, session_id):
            for record in await self.list_for_session(session_id):
                if record.admitted_generation == generation and record.state == AdmissionState.ADMITTED:
                    updated = validated_copy(record, state=AdmissionState.CANCELLED, cancelled_at=cancelled_at)
                    atomic_write_model(self.layout.admission(session_id, record.admission_id), updated)
                    changed.append(updated)
        return tuple(changed)


class FileSystemSessionControlRepository(_RepositoryBase):
    async def append(self, command: SessionControlCommand) -> SessionControlCommand:
        async with self.locks.hold(self.root, command.session_id):
            existing = await self.get_by_idempotency_key(command.session_id, command.idempotency_key)
            if existing is not None:
                return existing
            records = await self._list_all(command.session_id)
            if any(item.sequence_number == command.sequence_number for item in records):
                raise InputRuntimeConflictError("duplicate control sequence")
            atomic_write_model(self.layout.control(command.session_id, command.control_id), command)
            return command

    async def _list_all(self, session_id: str) -> tuple[SessionControlCommand, ...]:
        records = list_models(self.layout.controls(session_id), SessionControlCommand)
        return tuple(sorted(records, key=lambda item: item.sequence_number))

    async def get_by_idempotency_key(self, session_id: str, idempotency_key: str) -> SessionControlCommand | None:
        return next((item for item in await self._list_all(session_id) if item.idempotency_key == idempotency_key.strip()), None)

    async def _replace(self, control_id: str, **updates: object) -> SessionControlCommand:
        sessions = self.layout.root / "sessions"
        if sessions.exists():
            for path in sorted(sessions.glob("*/controls/*.json")):
                record = read_model(path, SessionControlCommand)
                if record.control_id == control_id:
                    async with self.locks.hold(self.root, record.session_id):
                        current = read_model(path, SessionControlCommand)
                        updated = validated_copy(current, **updates)
                        atomic_write_model(path, updated)
                        return updated
        raise InputRuntimeNotFoundError(control_id)

    async def acknowledge(self, control_id: str, *, acknowledged_at: datetime) -> SessionControlCommand:
        return await self._replace(control_id, state=ControlState.ACKNOWLEDGED, acknowledged_at=acknowledged_at)

    async def apply(self, control_id: str, *, applied_at: datetime) -> SessionControlCommand:
        record = await self._replace(control_id)
        return await self._replace(control_id, state=ControlState.APPLIED, acknowledged_at=record.acknowledged_at or applied_at, applied_at=applied_at)

    async def reject(self, control_id: str, *, rejection_code: str) -> SessionControlCommand:
        return await self._replace(control_id, state=ControlState.REJECTED, rejection_code=rejection_code)

    async def list_pending(self, session_id: str, *, generation: int) -> tuple[SessionControlCommand, ...]:
        return tuple(item for item in await self._list_all(session_id) if item.generation == generation and item.state in {ControlState.QUEUED, ControlState.ACKNOWLEDGED})

    async def cancel_generation(self, session_id: str, *, generation: int, reason_code: str) -> tuple[SessionControlCommand, ...]:
        changed = []
        async with self.locks.hold(self.root, session_id):
            for record in await self._list_all(session_id):
                if record.generation == generation and record.state in {ControlState.QUEUED, ControlState.ACKNOWLEDGED}:
                    updated = validated_copy(record, state=ControlState.CANCELLED, acknowledged_at=None, rejection_code=None)
                    atomic_write_model(self.layout.control(session_id, record.control_id), updated)
                    changed.append(updated)
        return tuple(changed)
