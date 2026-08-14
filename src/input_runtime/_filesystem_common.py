"""Shared filesystem repository primitives and recoverable indexes."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, TypeVar

from pydantic import BaseModel, ConfigDict

from .coordination import GLOBAL_SESSION_LOCKS, SessionLockRegistry
from .errors import InputRuntimeConflictError
from .serialization import atomic_write_model, read_model, storage_key

ModelT = TypeVar("ModelT", bound=BaseModel)


def validated_copy(record: ModelT, **updates: object) -> ModelT:
    data = record.model_dump(mode="json")
    data.update(updates)
    return type(record).model_validate(data)


class _IndexPointer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_type: str
    record_id: str
    session_id: str
    cycle_id: str | None = None
    relative_path: str


class _Layout:
    def __init__(self, root: Path) -> None:
        self.root = root / "input-runtime"

    def session_dir(self, session_id: str) -> Path:
        return self.root / "sessions" / storage_key(session_id)

    def cycle_dir(self, cycle_id: str) -> Path:
        return self.root / "cycles" / storage_key(cycle_id)

    def state(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "state.json"

    def admissions(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "admissions"

    def admission(self, session_id: str, record_id: str) -> Path:
        return self.admissions(session_id) / f"{storage_key(record_id)}.json"

    def inbox(self, cycle_id: str) -> Path:
        return self.cycle_dir(cycle_id) / "inbox"

    def inbox_item(self, cycle_id: str, record_id: str) -> Path:
        return self.inbox(cycle_id) / f"{storage_key(record_id)}.json"

    def controls(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "controls"

    def control(self, session_id: str, record_id: str) -> Path:
        return self.controls(session_id) / f"{storage_key(record_id)}.json"

    def snapshot(self, cycle_id: str) -> Path:
        return self.cycle_dir(cycle_id) / "snapshot.json"

    def revisions(self, cycle_id: str) -> Path:
        return self.cycle_dir(cycle_id) / "context-revisions"

    def revision(self, cycle_id: str, record_id: str) -> Path:
        return self.revisions(cycle_id) / f"{storage_key(record_id)}.json"

    def emissions(self, cycle_id: str) -> Path:
        return self.cycle_dir(cycle_id) / "emissions"

    def emission(self, cycle_id: str, record_id: str) -> Path:
        return self.emissions(cycle_id) / f"{storage_key(record_id)}.json"

    def finalizations(self, cycle_id: str) -> Path:
        return self.cycle_dir(cycle_id) / "finalizations"

    def finalization(self, cycle_id: str, record_id: str) -> Path:
        return self.finalizations(cycle_id) / f"{storage_key(record_id)}.json"

    def index(self, kind: str, key: str) -> Path:
        return self.root / "indexes" / kind / f"{storage_key(key)}.json"

    def cycle_authority(self, cycle_id: str) -> Path:
        return self.index("cycle-authority", cycle_id)

    def record_index(self, record_type: str, record_id: str) -> Path:
        return self.index(f"records-{record_type}", record_id)

    def admission_input(self, input_batch_id: str) -> Path:
        return self.index("admission-by-input", input_batch_id)

    def inbox_admission(self, admission_id: str) -> Path:
        return self.index("inbox-by-admission", admission_id)

    def inbox_input(self, input_batch_id: str) -> Path:
        return self.index("inbox-by-input", input_batch_id)


class _RepositoryBase:
    def __init__(
        self,
        *,
        root: Path,
        locks: SessionLockRegistry = GLOBAL_SESSION_LOCKS,
    ) -> None:
        self.root = root
        self.layout = _Layout(root)
        self.locks = locks

    @staticmethod
    def _all_json(directory: Path) -> Iterable[Path]:
        return sorted(directory.rglob("*.json")) if directory.exists() else ()

    def _pointer(
        self,
        record_type: str,
        record_id: str,
        session_id: str,
        path: Path,
        cycle_id: str | None = None,
    ) -> _IndexPointer:
        return _IndexPointer(
            record_type=record_type,
            record_id=record_id,
            session_id=session_id,
            cycle_id=cycle_id,
            relative_path=str(path.relative_to(self.layout.root)),
        )

    def _write_pointer(self, path: Path, pointer: _IndexPointer) -> None:
        atomic_write_model(path, pointer)

    def _read_pointer(self, path: Path) -> _IndexPointer | None:
        if not path.exists():
            return None
        try:
            return read_model(path, _IndexPointer)
        except Exception:
            return None

    def _pointer_record_path(self, pointer: _IndexPointer) -> Path:
        return self.layout.root / pointer.relative_path

    def _ensure_cycle_authority(self, cycle_id: str, session_id: str) -> None:
        path = self.layout.cycle_authority(cycle_id)
        pointer = self._read_pointer(path)
        if pointer is not None and pointer.session_id != session_id:
            raise InputRuntimeConflictError("cycle belongs to another session")
        if pointer is None:
            self._write_pointer(
                path,
                self._pointer(
                    "cycle",
                    cycle_id,
                    session_id,
                    self.layout.cycle_dir(cycle_id),
                    cycle_id,
                ),
            )
