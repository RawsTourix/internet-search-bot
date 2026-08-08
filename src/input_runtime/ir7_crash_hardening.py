"""Crash-window hardening layered over the IR-7 filesystem commands."""

from __future__ import annotations

from ._filesystem_common import validated_copy
from .ir7_filesystem import FileSystemFinalizationRepository as _IR7Repository
from .models import ActiveCycleSnapshot, CycleStatus, FinalizationState
from .serialization import atomic_write_model, read_model


class FileSystemFinalizationRepository(_IR7Repository):
    """Repair a partial terminal snapshot when terminal authority later aborts.

    Filesystem terminal commit writes snapshot -> session -> finalization marker
    while one exact-session lock is held. If the process dies after the first
    write, terminal authority never existed. A late durable input/control may
    therefore win after recreation; in that case the stale terminal snapshot
    must converge back to RUNNING instead of surviving as false evidence.
    """

    def _write_abort_locked(
        self,
        record,
        *,
        state: FinalizationState,
        reason: str,
        now,
    ):
        session_before = self._session_state(record.session_id)
        repair_partial_snapshot = (
            session_before.generation == record.generation
            and session_before.active_cycle_id == record.cycle_id
            and session_before.cycle_status == CycleStatus.FINALIZING
            and session_before.finalization_id == record.finalization_id
        )
        aborted = super()._write_abort_locked(
            record,
            state=state,
            reason=reason,
            now=now,
        )
        if not repair_partial_snapshot:
            return aborted

        snapshot_path = self.layout.snapshot(record.cycle_id)
        if not snapshot_path.exists():
            return aborted
        snapshot = read_model(snapshot_path, ActiveCycleSnapshot)
        if (
            snapshot.session_id != record.session_id
            or snapshot.generation != record.generation
            or snapshot.status
            not in {CycleStatus.DONE, CycleStatus.ERROR, CycleStatus.CANCELLED}
        ):
            return aborted
        repaired = validated_copy(
            snapshot,
            status=CycleStatus.RUNNING,
            waiting_question=None,
            pause_reason=None,
            interruption_reason=None,
            cancellation_reason_code=None,
            snapshot_revision=snapshot.snapshot_revision + 1,
            updated_at=max(snapshot.updated_at, now),
        )
        atomic_write_model(snapshot_path, repaired)
        return aborted
