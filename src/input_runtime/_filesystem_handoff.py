"""Filesystem adapter for the storage-neutral runtime handoff port."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .coordination import SessionLockRegistry
from .errors import InputRuntimeConflictError
from .handoff import RuntimeHandoffRecord, RuntimeHandoffState
from .serialization import atomic_write_model, read_model, storage_key


class FileSystemRuntimeHandoffRepository:
    """Durable IR-3 handoff markers with session-scoped transition fencing."""

    def __init__(self, *, root: Path, locks: SessionLockRegistry) -> None:
        self.root = Path(root)
        self.locks = locks

    def _path(self, admission_id: str) -> Path:
        return (
            self.root
            / "input-runtime"
            / "runtime-handoffs"
            / f"{storage_key(admission_id)}.json"
        )

    async def get(self, admission_id: str) -> RuntimeHandoffRecord | None:
        path = self._path(admission_id)
        return read_model(path, RuntimeHandoffRecord) if path.exists() else None

    @staticmethod
    def _same_relation(
        existing: RuntimeHandoffRecord,
        candidate: RuntimeHandoffRecord,
    ) -> bool:
        return (
            existing.admission_id,
            existing.session_id,
            existing.input_batch_id,
            existing.cycle_id,
        ) == (
            candidate.admission_id,
            candidate.session_id,
            candidate.input_batch_id,
            candidate.cycle_id,
        )

    async def begin(
        self,
        candidate: RuntimeHandoffRecord,
    ) -> RuntimeHandoffRecord:
        async with self.locks.hold(self.root, candidate.session_id):
            path = self._path(candidate.admission_id)
            if path.exists():
                current = read_model(path, RuntimeHandoffRecord)
                if not self._same_relation(current, candidate):
                    raise InputRuntimeConflictError(
                        "runtime handoff relation changed"
                    )
                return current
            atomic_write_model(path, candidate)
            return candidate

    async def complete(
        self,
        admission_id: str,
        *,
        handoff_token: str,
        completed_at: datetime,
    ) -> RuntimeHandoffRecord:
        current = await self.get(admission_id)
        if current is None:
            raise InputRuntimeConflictError("runtime handoff marker is missing")
        async with self.locks.hold(self.root, current.session_id):
            path = self._path(admission_id)
            current = read_model(path, RuntimeHandoffRecord)
            if current.handoff_token != handoff_token:
                raise InputRuntimeConflictError("runtime handoff token mismatch")
            if current.state == RuntimeHandoffState.COMPLETED:
                return current
            if current.state == RuntimeHandoffState.AMBIGUOUS:
                return current
            updated = current.model_copy(
                update={
                    "state": RuntimeHandoffState.COMPLETED,
                    "completed_at": completed_at,
                }
            )
            updated = RuntimeHandoffRecord.model_validate(
                updated.model_dump(mode="python")
            )
            atomic_write_model(path, updated)
            return updated

    async def mark_ambiguous(
        self,
        admission_id: str,
        *,
        handoff_token: str,
        ambiguous_at: datetime,
        error_code: str,
    ) -> RuntimeHandoffRecord:
        current = await self.get(admission_id)
        if current is None:
            raise InputRuntimeConflictError("runtime handoff marker is missing")
        async with self.locks.hold(self.root, current.session_id):
            path = self._path(admission_id)
            current = read_model(path, RuntimeHandoffRecord)
            if current.handoff_token != handoff_token:
                raise InputRuntimeConflictError("runtime handoff token mismatch")
            if current.state in {
                RuntimeHandoffState.AMBIGUOUS,
                RuntimeHandoffState.COMPLETED,
            }:
                return current
            updated = current.model_copy(
                update={
                    "state": RuntimeHandoffState.AMBIGUOUS,
                    "ambiguous_at": ambiguous_at,
                    "error_code": error_code,
                }
            )
            updated = RuntimeHandoffRecord.model_validate(
                updated.model_dump(mode="python")
            )
            atomic_write_model(path, updated)
            return updated
