"""Crash-safe IR-3 boundary between admission and Agent Runtime invocation."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .coordination import SessionLockRegistry
from .errors import InputRuntimeConflictError
from .serialization import atomic_write_model, read_model, storage_key


class RuntimeHandoffState(str, Enum):
    HANDED_OFF = "handed_off"
    COMPLETED = "completed"
    AMBIGUOUS = "ambiguous"


class RuntimeHandoffRecord(BaseModel):
    """Durable evidence that execution crossed into the side-effecting runtime."""

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    admission_id: str
    session_id: str
    input_batch_id: str
    cycle_id: str
    handoff_token: str
    state: RuntimeHandoffState = RuntimeHandoffState.HANDED_OFF
    handed_off_at: datetime
    completed_at: datetime | None = None
    ambiguous_at: datetime | None = None
    error_code: str | None = None

    @field_validator(
        "admission_id",
        "session_id",
        "input_batch_id",
        "cycle_id",
        "handoff_token",
        mode="before",
    )
    @classmethod
    def normalize_identity(cls, value: str) -> str:
        normalized = str(value).strip()
        if not normalized:
            raise ValueError("handoff identity must not be empty")
        return normalized

    @field_validator(
        "handed_off_at",
        "completed_at",
        "ambiguous_at",
        mode="before",
    )
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("handoff timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_record(self) -> "RuntimeHandoffRecord":
        if self.state == RuntimeHandoffState.HANDED_OFF:
            if self.completed_at is not None or self.ambiguous_at is not None:
                raise ValueError("open handoff cannot have terminal timestamps")
            if self.error_code is not None:
                raise ValueError("open handoff cannot have error_code")
        elif self.state == RuntimeHandoffState.COMPLETED:
            if self.completed_at is None or self.ambiguous_at is not None:
                raise ValueError("completed handoff timestamp mismatch")
            if self.error_code is not None:
                raise ValueError("completed handoff cannot have error_code")
        elif self.state == RuntimeHandoffState.AMBIGUOUS:
            if self.ambiguous_at is None or self.completed_at is not None:
                raise ValueError("ambiguous handoff timestamp mismatch")
            if not self.error_code:
                raise ValueError("ambiguous handoff requires error_code")
        return self


class FileSystemRuntimeHandoffStore:
    """Small durable sidecar owned by IR-3, independent from IR-4 snapshots."""

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
