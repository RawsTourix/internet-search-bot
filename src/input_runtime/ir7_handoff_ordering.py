"""IR-7 RuntimeHandoff-before-terminal filesystem coordination.

The normal admitted-run terminal path binds one exact durable RuntimeHandoff to
one existing CycleFinalizationRecord.  The final terminal recheck, handoff
completion, terminal snapshot/session writes and TERMINAL_COMMITTED marker then
share one exact-session coordination boundary.

No application-layer code depends on filesystem paths or locks.  A future SQL
adapter can map the same command to one transaction/row-lock boundary.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from . import ir7_filesystem as finalization_writes
from ._filesystem_common import validated_copy
from ._filesystem_handoff import (
    FileSystemRuntimeHandoffRepository as _BaseRuntimeHandoffRepository,
)
from .errors import InputRuntimeConflictError, InputRuntimeNotFoundError
from .handoff import RuntimeHandoffRecord, RuntimeHandoffState
from .ir7_crash_hardening import (
    FileSystemFinalizationRepository as _BaseFinalizationRepository,
)
from .models import CycleFinalizationRecord, CycleStatus, FinalizationState
from .serialization import atomic_write_model, list_models, read_model, storage_key


class _FinalizationHandoffAuthority(BaseModel):
    """Immutable infrastructure relation; not a second finalization state machine."""

    model_config = ConfigDict(extra="forbid")

    finalization_id: str
    session_id: str
    cycle_id: str
    admission_id: str
    handoff_token: str
    bound_at: datetime


class FileSystemRuntimeHandoffRepository(_BaseRuntimeHandoffRepository):
    """Expose one lock-aware completion primitive for coordinated IR-7 commit."""

    def _get_locked(self, admission_id: str) -> RuntimeHandoffRecord | None:
        path = self._path(admission_id)
        return read_model(path, RuntimeHandoffRecord) if path.exists() else None

    def _complete_locked(
        self,
        admission_id: str,
        *,
        handoff_token: str,
        completed_at: datetime,
        expected_session_id: str | None = None,
        expected_cycle_id: str | None = None,
    ) -> RuntimeHandoffRecord:
        current = self._get_locked(admission_id)
        if current is None:
            raise InputRuntimeConflictError("runtime handoff marker is missing")
        if current.handoff_token != handoff_token:
            raise InputRuntimeConflictError("runtime handoff token mismatch")
        if (
            expected_session_id is not None
            and current.session_id != expected_session_id
        ):
            raise InputRuntimeConflictError("runtime handoff session mismatch")
        if expected_cycle_id is not None and current.cycle_id != expected_cycle_id:
            raise InputRuntimeConflictError("runtime handoff cycle mismatch")
        if current.state == RuntimeHandoffState.COMPLETED:
            return current
        if current.state == RuntimeHandoffState.AMBIGUOUS:
            return current
        updated = validated_copy(
            current,
            state=RuntimeHandoffState.COMPLETED,
            completed_at=completed_at,
        )
        atomic_write_model(self._path(admission_id), updated)
        return updated

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
            return self._complete_locked(
                admission_id,
                handoff_token=handoff_token,
                completed_at=completed_at,
                expected_session_id=current.session_id,
                expected_cycle_id=current.cycle_id,
            )


class FileSystemFinalizationRepository(_BaseFinalizationRepository):
    """Coordinate exact handoff completion before any terminal delivery authority."""

    def __init__(self, *, root, locks, handoffs=None) -> None:
        super().__init__(root=root, locks=locks)
        self.handoffs = handoffs or FileSystemRuntimeHandoffRepository(
            root=root,
            locks=locks,
        )

    def _handoff_authority_path(self, cycle_id: str, finalization_id: str):
        return (
            self.layout.cycle_dir(cycle_id)
            / "finalization-handoff-authority"
            / f"{storage_key(finalization_id)}.json"
        )

    def _handoff_authority(
        self,
        record: CycleFinalizationRecord,
    ) -> _FinalizationHandoffAuthority | None:
        path = self._handoff_authority_path(
            record.cycle_id,
            record.finalization_id,
        )
        if not path.exists():
            return None
        authority = read_model(path, _FinalizationHandoffAuthority)
        if (
            authority.finalization_id != record.finalization_id
            or authority.session_id != record.session_id
            or authority.cycle_id != record.cycle_id
        ):
            raise InputRuntimeConflictError(
                "finalization runtime handoff authority changed"
            )
        return authority

    async def bind_runtime_handoff_authority(
        self,
        *,
        finalization_id: str,
        session_id: str,
        cycle_id: str,
        admission_id: str,
        handoff_token: str,
        bound_at: datetime,
    ) -> None:
        candidate = _FinalizationHandoffAuthority(
            finalization_id=finalization_id,
            session_id=session_id,
            cycle_id=cycle_id,
            admission_id=admission_id,
            handoff_token=handoff_token,
            bound_at=bound_at,
        )
        async with self.locks.hold(self.root, session_id):
            marker = self.handoffs._get_locked(admission_id)
            if marker is None:
                raise InputRuntimeConflictError("runtime handoff marker is missing")
            if (
                marker.session_id != session_id
                or marker.cycle_id != cycle_id
                or marker.handoff_token != handoff_token
            ):
                raise InputRuntimeConflictError(
                    "runtime handoff finalization relation changed"
                )
            if marker.state != RuntimeHandoffState.HANDED_OFF:
                raise InputRuntimeConflictError(
                    "runtime handoff is not active for finalization"
                )
            path = self._handoff_authority_path(cycle_id, finalization_id)
            if path.exists():
                existing = read_model(path, _FinalizationHandoffAuthority)
                immutable_relation = (
                    existing.finalization_id,
                    existing.session_id,
                    existing.cycle_id,
                    existing.admission_id,
                    existing.handoff_token,
                )
                candidate_relation = (
                    candidate.finalization_id,
                    candidate.session_id,
                    candidate.cycle_id,
                    candidate.admission_id,
                    candidate.handoff_token,
                )
                if immutable_relation != candidate_relation:
                    raise InputRuntimeConflictError(
                        "finalization runtime handoff binding changed"
                    )
                return
            atomic_write_model(path, candidate)

    async def commit_terminal_authority(
        self,
        finalization_id: str,
        *,
        terminal_status: CycleStatus,
        committed_at: datetime,
    ) -> CycleFinalizationRecord:
        record = await self.get(finalization_id)
        if record is None:
            raise InputRuntimeNotFoundError(finalization_id)
        authority = self._handoff_authority(record)
        if authority is None:
            # Compatibility/direct repository tests created before the admitted
            # RuntimeHandoff integration retain the pre-corrective IR-7 command.
            # Normal production admitted runs always bind an authority at PREPARED.
            return await super().commit_terminal_authority(
                finalization_id,
                terminal_status=terminal_status,
                committed_at=committed_at,
            )

        async with self.locks.hold(self.root, record.session_id):
            current = await self.get(finalization_id)
            if current is None:
                raise InputRuntimeNotFoundError(finalization_id)
            authority = self._handoff_authority(current)
            if authority is None:
                raise InputRuntimeConflictError(
                    "finalization lost runtime handoff authority"
                )
            if current.state == FinalizationState.TERMINAL_COMMITTED:
                marker = self.handoffs._complete_locked(
                    authority.admission_id,
                    handoff_token=authority.handoff_token,
                    completed_at=committed_at,
                    expected_session_id=current.session_id,
                    expected_cycle_id=current.cycle_id,
                )
                if marker.state != RuntimeHandoffState.COMPLETED:
                    raise InputRuntimeConflictError(
                        "terminal finalization has incomplete runtime handoff"
                    )
                return current
            if current.state in {
                FinalizationState.ABORTED_NEW_INPUT,
                FinalizationState.ABORTED_CONTROL,
                FinalizationState.FAILED_TERMINAL,
            }:
                return current
            if current.state != FinalizationState.OUTPUT_READY:
                raise InputRuntimeConflictError(
                    "output must be ready before terminal commit"
                )

            session = self._session_state(current.session_id)
            abort_state, reason = self._abort_kind(session, current)
            partial_terminal = (
                session.generation == current.generation
                and session.active_cycle_id == current.cycle_id
                and session.finalization_id == current.finalization_id
                and session.cycle_status == terminal_status
                and session.active_cycle_accepted_through_sequence
                == current.expected_accepted_sequence
                and session.active_cycle_applied_through_sequence
                == current.expected_applied_sequence
                and session.pending_control_sequence
                == current.expected_control_sequence
                and session.applied_control_sequence
                == current.expected_control_sequence
            )
            if abort_state is not None and not partial_terminal:
                return self._write_abort_locked(
                    current,
                    state=abort_state,
                    reason=reason or "terminal_authority_changed",
                    now=committed_at,
                )
            if not partial_terminal and (
                session.cycle_status != CycleStatus.FINALIZING
                or session.finalization_id != current.finalization_id
            ):
                return self._write_abort_locked(
                    current,
                    state=FinalizationState.ABORTED_CONTROL,
                    reason="finalization_ownership_lost",
                    now=committed_at,
                )

            # The second authoritative recheck above is the last point at which
            # late input/control may abort and continue the same cycle.  Only now
            # is it valid to declare the side-effecting runtime invocation done.
            marker = self.handoffs._complete_locked(
                authority.admission_id,
                handoff_token=authority.handoff_token,
                completed_at=committed_at,
                expected_session_id=current.session_id,
                expected_cycle_id=current.cycle_id,
            )
            if marker.state != RuntimeHandoffState.COMPLETED:
                raise InputRuntimeConflictError(
                    "runtime handoff did not complete before terminal authority"
                )

            self._sync_snapshot_terminal_locked(
                current,
                terminal_status=terminal_status,
                committed_at=committed_at,
            )
            if not partial_terminal:
                terminal_session = validated_copy(
                    session,
                    cycle_status=terminal_status,
                    finalization_id=current.finalization_id,
                    revision=session.revision + 1,
                    updated_at=max(session.updated_at, committed_at),
                )
                finalization_writes.atomic_write_model(
                    self.layout.state(current.session_id),
                    terminal_session,
                )
            committed = validated_copy(
                current,
                state=FinalizationState.TERMINAL_COMMITTED,
                updated_at=max(current.updated_at, committed_at),
            )
            # Final delivery authority is deliberately the last durable write.
            finalization_writes.atomic_write_model(
                self.layout.finalization(
                    current.cycle_id,
                    current.finalization_id,
                ),
                committed,
            )
            return committed

    async def output_delivery_allowed(
        self,
        *,
        session_id: str,
        cycle_id: str,
        output_batch_id: str,
    ) -> bool:
        if not session_id or not cycle_id or not output_batch_id:
            return False
        records = list_models(
            self.layout.finalizations(cycle_id),
            CycleFinalizationRecord,
        )
        matching = [
            item
            for item in records
            if item.session_id == session_id
            and item.cycle_id == cycle_id
            and item.output_batch_id == output_batch_id
        ]
        if len(matching) != 1:
            return False
        record = matching[0]
        if record.state != FinalizationState.TERMINAL_COMMITTED:
            return False
        authority = self._handoff_authority(record)
        if authority is None:
            return True
        marker = await self.handoffs.get(authority.admission_id)
        return bool(
            marker is not None
            and marker.session_id == session_id
            and marker.cycle_id == cycle_id
            and marker.handoff_token == authority.handoff_token
            and marker.state == RuntimeHandoffState.COMPLETED
        )
