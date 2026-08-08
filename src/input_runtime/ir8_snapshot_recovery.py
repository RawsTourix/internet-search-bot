"""Strict IR-8 snapshot/session interruption projection."""

from __future__ import annotations

from datetime import datetime

from ._filesystem_common import validated_copy
from .errors import InputRuntimeConflictError, InputRuntimeNotFoundError
from .ir8_filesystem import FileSystemActiveCycleSnapshotRepository as _IR8SnapshotRepository
from .models import ActiveCycleSnapshot, CycleStatus, SessionInputRuntimeState
from .serialization import atomic_write_model, read_model


class FileSystemActiveCycleSnapshotRepository(_IR8SnapshotRepository):
    async def mark_recovery_interrupted(
        self,
        *,
        session_id: str,
        cycle_id: str,
        generation: int,
        reason_code: str,
        interrupted_at: datetime,
    ) -> ActiveCycleSnapshot:
        async with self.locks.hold(self.root, session_id):
            snapshot_path = self.layout.snapshot(cycle_id)
            state_path = self.layout.state(session_id)
            if not snapshot_path.exists() or not state_path.exists():
                raise InputRuntimeNotFoundError(
                    "recovery interruption requires session and snapshot"
                )
            snapshot = read_model(snapshot_path, ActiveCycleSnapshot)
            state = read_model(state_path, SessionInputRuntimeState)
            if (
                snapshot.session_id != session_id
                or snapshot.generation != generation
                or state.generation != generation
                or state.active_cycle_id != cycle_id
            ):
                raise InputRuntimeConflictError(
                    "recovery interruption lost cycle authority"
                )
            if snapshot.status in {
                CycleStatus.DONE,
                CycleStatus.ERROR,
                CycleStatus.CANCELLED,
            }:
                raise InputRuntimeConflictError(
                    "terminal snapshot cannot be reclassified as interrupted"
                )
            updated_snapshot = validated_copy(
                snapshot,
                status=CycleStatus.INTERRUPTED,
                waiting_question=None,
                pause_reason=None,
                interruption_reason=reason_code,
                cancellation_reason_code=None,
                snapshot_revision=snapshot.snapshot_revision + 1,
                updated_at=max(snapshot.updated_at, interrupted_at),
            )
            atomic_write_model(snapshot_path, updated_snapshot)
            if (
                state.cycle_status != CycleStatus.INTERRUPTED
                or state.finalization_id is not None
            ):
                updated_state = validated_copy(
                    state,
                    cycle_status=CycleStatus.INTERRUPTED,
                    finalization_id=None,
                    revision=state.revision + 1,
                    updated_at=max(state.updated_at, interrupted_at),
                )
                atomic_write_model(state_path, updated_state)
            return updated_snapshot
