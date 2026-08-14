"""Filesystem-only startup discovery and reconciliation commands for IR-8.

The application recovery coordinator consumes these commands through structural
ports.  Filesystem layout, scans and atomic multi-file repair remain confined to
this adapter so a PostgreSQL implementation can replace them with indexed
queries and row-locked transactions.
"""

from __future__ import annotations

from datetime import datetime

from ._filesystem_common import validated_copy
from ._filesystem_identity_recovery_cycle import (
    FileSystemActiveCycleSnapshotRepository as _SnapshotRepository,
)
from .errors import InputRuntimeConflictError, InputRuntimeNotFoundError
from .handoff import RuntimeHandoffRecord, RuntimeHandoffState
from .ir5_filesystem_controls import (
    FileSystemSessionControlRepository as _ControlRepository,
)
from .ir7_admission_ordering import (
    FileSystemInputAdmissionRepository as _AdmissionRepository,
)
from .ir7_filesystem import _FinalResultEnvelope
from .ir7_handoff_ordering import (
    FileSystemFinalizationRepository as _FinalizationRepository,
    FileSystemRuntimeHandoffRepository as _HandoffRepository,
)
from .models import (
    ActiveCycleSnapshot,
    CycleFinalizationRecord,
    CycleStatus,
    FinalizationState,
    SessionInputRuntimeState,
)
from .serialization import atomic_write_model, read_model


class FileSystemInputAdmissionRepository(_AdmissionRepository):
    """Expose startup-only authoritative admission discovery/repair."""

    async def list_all_for_recovery(self):
        rows = self._scan_all()
        return tuple(
            sorted(
                rows,
                key=lambda item: (
                    item.session_id,
                    item.session_sequence,
                    item.target_cycle_id,
                    item.cycle_sequence,
                    item.admission_id,
                ),
            )
        )

    async def recover_session_authority(
        self,
        session_id: str,
    ) -> SessionInputRuntimeState | None:
        async with self.locks.hold_identity_then_session(self.root, session_id):
            state_path = self.layout.state(session_id)
            rows = await self.list_for_session(session_id)
            if not state_path.exists():
                if rows:
                    raise InputRuntimeConflictError(
                        "admission history exists without session runtime state"
                    )
                return None
            state = read_model(state_path, SessionInputRuntimeState)
            repaired = await self._repair_from_authoritative_admissions(
                state_path,
                state,
            )
            # Re-publish missing/dangling stable indexes only after the immutable
            # history has passed the deterministic sequence checks above.
            for row in rows:
                self._restore_indexes(row)
            return repaired


class FileSystemSessionControlRepository(_ControlRepository):
    """Validate and repair the durable control frontier once at startup."""

    async def recover_session_authority(
        self,
        session_id: str,
    ) -> SessionInputRuntimeState | None:
        async with self.locks.hold_identity_then_session(self.root, session_id):
            state_path = self.layout.state(session_id)
            rows = await self.list_for_session(session_id)
            if not state_path.exists():
                if rows:
                    raise InputRuntimeConflictError(
                        "control history exists without session runtime state"
                    )
                return None
            state = read_model(state_path, SessionInputRuntimeState)
            repaired = await self._repair_control_frontier_locked(
                state_path,
                state,
            )
            ordered = sorted(rows, key=lambda item: item.sequence_number)
            sequences = [item.sequence_number for item in ordered]
            if sequences and sequences != list(range(1, sequences[-1] + 1)):
                raise InputRuntimeConflictError(
                    "gap in durable control sequence for session"
                )
            if repaired.applied_control_sequence > repaired.pending_control_sequence:
                raise InputRuntimeConflictError(
                    "applied control watermark exceeds pending control watermark"
                )
            for row in ordered:
                self._restore_indexes(row)
            return repaired


class FileSystemRuntimeHandoffRepository(_HandoffRepository):
    """Whole-store handoff discovery is intentionally startup-only."""

    async def list_for_recovery(self) -> tuple[RuntimeHandoffRecord, ...]:
        directory = self.root / "input-runtime" / "runtime-handoffs"
        if not directory.exists():
            return ()
        rows = tuple(
            read_model(path, RuntimeHandoffRecord)
            for path in sorted(directory.glob("*.json"))
        )
        return tuple(
            sorted(
                rows,
                key=lambda item: (
                    item.session_id,
                    item.cycle_id,
                    item.handed_off_at,
                    item.admission_id,
                ),
            )
        )

    async def list_nonterminal_for_recovery(self) -> tuple[RuntimeHandoffRecord, ...]:
        return tuple(
            item
            for item in await self.list_for_recovery()
            if item.state != RuntimeHandoffState.COMPLETED
        )


class FileSystemActiveCycleSnapshotRepository(_SnapshotRepository):
    """Apply restart classification without inventing a new snapshot model."""

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
            if state.cycle_status != CycleStatus.INTERRUPTED:
                updated_state = validated_copy(
                    state,
                    cycle_status=CycleStatus.INTERRUPTED,
                    revision=state.revision + 1,
                    updated_at=max(state.updated_at, interrupted_at),
                )
                atomic_write_model(state_path, updated_state)
            return updated_snapshot


class FileSystemFinalizationRepository(_FinalizationRepository):
    """Expose exact persisted finalization evidence to startup recovery."""

    async def list_for_recovery(self) -> tuple[CycleFinalizationRecord, ...]:
        states = {
            FinalizationState.PREPARED,
            FinalizationState.RESULT_PERSISTED,
            FinalizationState.OUTPUT_READY,
            FinalizationState.FAILED_RECOVERABLE,
            FinalizationState.TERMINAL_COMMITTED,
        }
        return tuple(
            sorted(
                (item for item in self._scan() if item.state in states),
                key=lambda item: (
                    item.session_id,
                    item.cycle_id,
                    item.updated_at,
                    item.finalization_id,
                ),
            )
        )

    async def get_runtime_handoff_for_recovery(
        self,
        finalization_id: str,
    ) -> RuntimeHandoffRecord | None:
        record = await self.get(finalization_id)
        if record is None:
            raise InputRuntimeNotFoundError(finalization_id)
        authority = self._handoff_authority(record)
        if authority is None:
            return None
        marker = await self.handoffs.get(authority.admission_id)
        if marker is None:
            raise InputRuntimeConflictError(
                "finalization references missing runtime handoff"
            )
        if (
            marker.session_id != record.session_id
            or marker.cycle_id != record.cycle_id
            or marker.handoff_token != authority.handoff_token
        ):
            raise InputRuntimeConflictError(
                "finalization/handoff identity conflict"
            )
        return marker

    async def load_result_payload_for_recovery(
        self,
        finalization_id: str,
    ) -> dict:
        record = await self.get(finalization_id)
        if record is None:
            raise InputRuntimeNotFoundError(finalization_id)
        if record.result_ref is None:
            raise InputRuntimeConflictError(
                "recoverable finalization has no persisted result reference"
            )
        result_path = self._result_path(record)
        if not result_path.exists():
            raise InputRuntimeConflictError(
                "persisted finalization result is missing"
            )
        envelope = read_model(result_path, _FinalResultEnvelope)
        if (
            envelope.finalization_id != record.finalization_id
            or envelope.result_ref != record.result_ref
        ):
            raise InputRuntimeConflictError(
                "persisted finalization result identity conflict"
            )
        return dict(envelope.payload)

    async def recheck_recoverable_authority(
        self,
        finalization_id: str,
        *,
        checked_at: datetime,
    ) -> CycleFinalizationRecord:
        stale = await self.get(finalization_id)
        if stale is None:
            raise InputRuntimeNotFoundError(finalization_id)
        async with self.locks.hold(self.root, stale.session_id):
            current = await self.get(finalization_id)
            if current is None:
                raise InputRuntimeNotFoundError(finalization_id)
            if current.state not in {
                FinalizationState.PREPARED,
                FinalizationState.RESULT_PERSISTED,
                FinalizationState.OUTPUT_READY,
            }:
                return current
            session = self._session_state(current.session_id)
            abort_state, reason = self._abort_kind(session, current)
            if abort_state is None:
                return current
            return self._write_abort_locked(
                current,
                state=abort_state,
                reason=reason or "startup_finalization_authority_changed",
                now=checked_at,
            )

    async def abandon_prepared_for_recovery(
        self,
        finalization_id: str,
        *,
        interrupted_at: datetime,
        reason_code: str,
    ) -> CycleFinalizationRecord:
        stale = await self.get(finalization_id)
        if stale is None:
            raise InputRuntimeNotFoundError(finalization_id)
        async with self.locks.hold(self.root, stale.session_id):
            current = await self.get(finalization_id)
            if current is None:
                raise InputRuntimeNotFoundError(finalization_id)
            if current.state != FinalizationState.PREPARED:
                return current
            return self._write_abort_locked(
                current,
                state=FinalizationState.ABORTED_CONTROL,
                reason=reason_code,
                now=interrupted_at,
            )

    async def repair_terminal_projection_for_recovery(
        self,
        finalization_id: str,
        *,
        repaired_at: datetime,
    ) -> CycleFinalizationRecord:
        record = await self.get(finalization_id)
        if record is None:
            raise InputRuntimeNotFoundError(finalization_id)
        if record.state != FinalizationState.TERMINAL_COMMITTED:
            raise InputRuntimeConflictError(
                "terminal projection repair requires terminal authority"
            )
        # Reuse the IR-7 command first so matching RuntimeHandoff completion is
        # validated.  Then repair only a lagging projection that still points at
        # this exact generation/cycle; never overwrite a later active cycle.
        current = await self.commit_terminal_authority(
            finalization_id,
            terminal_status=CycleStatus.DONE,
            committed_at=repaired_at,
        )
        async with self.locks.hold(self.root, record.session_id):
            state_path = self.layout.state(record.session_id)
            if not state_path.exists():
                raise InputRuntimeConflictError(
                    "terminal authority has no session runtime state"
                )
            state = read_model(state_path, SessionInputRuntimeState)
            if (
                state.generation == record.generation
                and state.active_cycle_id == record.cycle_id
            ):
                self._sync_snapshot_terminal_locked(
                    record,
                    terminal_status=CycleStatus.DONE,
                    committed_at=repaired_at,
                )
                if (
                    state.cycle_status != CycleStatus.DONE
                    or state.finalization_id != record.finalization_id
                ):
                    repaired = validated_copy(
                        state,
                        cycle_status=CycleStatus.DONE,
                        finalization_id=record.finalization_id,
                        revision=state.revision + 1,
                        updated_at=max(state.updated_at, repaired_at),
                    )
                    atomic_write_model(state_path, repaired)
        return current
