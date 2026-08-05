"""Shared filesystem repository primitives."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, TypeVar

from pydantic import BaseModel, ConfigDict

from .coordination import GLOBAL_SESSION_LOCKS, SessionLockRegistry
from .models import (
    ActiveCycleSnapshot, AgentEmission, CycleContextRevision,
    CycleFinalizationRecord, CycleInboxItem, InputAdmissionRecord,
    SessionControlCommand, SessionInputRuntimeState,
)
from .serialization import atomic_write_model, read_model, storage_key

ModelT = TypeVar("ModelT", bound=BaseModel)


def validated_copy(record: ModelT, **updates: object) -> ModelT:
    payload = record.model_dump(mode="json")
    payload.update(updates)
    return type(record).model_validate(payload)


class _DeliveryClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emission_id: str
    claim_token: str


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

    def admission(self, session_id: str, admission_id: str) -> Path:
        return self.admissions(session_id) / f"{storage_key(admission_id)}.json"

    def inbox(self, cycle_id: str) -> Path:
        return self.cycle_dir(cycle_id) / "inbox"

    def inbox_item(self, cycle_id: str, inbox_item_id: str) -> Path:
        return self.inbox(cycle_id) / f"{storage_key(inbox_item_id)}.json"

    def controls(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "controls"

    def control(self, session_id: str, control_id: str) -> Path:
        return self.controls(session_id) / f"{storage_key(control_id)}.json"

    def snapshot(self, cycle_id: str) -> Path:
        return self.cycle_dir(cycle_id) / "snapshot.json"

    def revisions(self, cycle_id: str) -> Path:
        return self.cycle_dir(cycle_id) / "context-revisions"

    def revision(self, cycle_id: str, revision_id: str) -> Path:
        return self.revisions(cycle_id) / f"{storage_key(revision_id)}.json"

    def emissions(self, cycle_id: str) -> Path:
        return self.cycle_dir(cycle_id) / "emissions"

    def emission(self, cycle_id: str, emission_id: str) -> Path:
        return self.emissions(cycle_id) / f"{storage_key(emission_id)}.json"

    def emission_claim(self, cycle_id: str, emission_id: str) -> Path:
        return self.emissions(cycle_id) / f"{storage_key(emission_id)}.claim.json"

    def finalizations(self, cycle_id: str) -> Path:
        return self.cycle_dir(cycle_id) / "finalizations"

    def finalization(self, cycle_id: str, finalization_id: str) -> Path:
        return self.finalizations(cycle_id) / f"{storage_key(finalization_id)}.json"


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
        if not directory.exists():
            return ()
        return sorted(directory.rglob("*.json"))
