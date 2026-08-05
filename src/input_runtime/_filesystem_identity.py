"""Globally fenced filesystem adapters for admission and inbox identities."""

from __future__ import annotations

from . import _filesystem_session as session_repository_module
from ._filesystem_common import validated_copy
from ._filesystem_cycle import FileSystemCycleInboxRepository as _CycleInboxBase
from ._filesystem_cycle import _same_inbox_relation
from ._filesystem_session import (
    FileSystemInputAdmissionRepository as _AdmissionBase,
)
from ._filesystem_session import TERMINAL_OR_IDLE, _same_admission_relation
from .errors import InputRuntimeConflictError, InputRuntimeNotFoundError
from .models import (
    AdmissionKind,
    CycleInboxItem,
    CycleStatus,
    InputAdmissionRecord,
    SessionInputRuntimeState,
)
from .serialization import read_model


def atomic_write_model(path, model):
    """Forward writes through the canonical session module test seam."""
    return session_repository_module.atomic_write_model(path, model)


class FileSystemInputAdmissionRepository(_AdmissionBase):
    """Admission repository with root-global identity fencing and repair."""

    def _repair_from_authoritative_admissions(
        self,
        state_path,
        state: SessionInputRuntimeState,
    ) -> SessionInputRuntimeState:
        admissions = list(self._scan())
        session_rows = sorted(
            (item for item in admissions if item.session_id == state.session_id),
            key=lambda item: item.session_sequence,
        )
        if not session_rows:
            return state

        sequences = [item.session_sequence for item in session_rows]
        if len(sequences) != len(set(sequences)):
            raise InputRuntimeConflictError("duplicate authoritative session sequence")
        if sequences != list(range(1, max(sequences) + 1)):
            raise InputRuntimeConflictError("gap in authoritative session sequence")

        accepted_session = max(sequences)
        generation_rows = [
            item
            for item in session_rows
            if item.admitted_generation == state.generation
        ]
        orphan_starts = [
            item
            for item in generation_rows
            if item.admission_kind == AdmissionKind.START_CYCLE
            and item.session_sequence > state.accepted_through_session_sequence
        ]

        active_cycle_id = state.active_cycle_id
        cycle_status = state.cycle_status
        active_context_revision_id = state.active_context_revision_id
        finalization_id = state.finalization_id
        active_applied = state.active_cycle_applied_through_sequence

        if orphan_starts:
            start = max(orphan_starts, key=lambda item: item.session_sequence)
            active_cycle_id = start.target_cycle_id
            cycle_status = CycleStatus.RUNNING
            active_context_revision_id = None
            finalization_id = None
            active_applied = 0

        active_rows = [
            item
            for item in generation_rows
            if active_cycle_id is not None
            and item.target_cycle_id == active_cycle_id
        ]
        active_accepted = max(
            (item.cycle_sequence for item in active_rows),
            default=0,
        )

        updates = {
            "accepted_through_session_sequence": accepted_session,
            "active_cycle_id": active_cycle_id,
            "cycle_status": cycle_status,
            "active_cycle_accepted_through_sequence": active_accepted,
            "active_cycle_applied_through_sequence": min(
                active_applied,
                active_accepted,
            ),
            "active_context_revision_id": active_context_revision_id,
            "finalization_id": finalization_id,
        }
        changed = any(getattr(state, key) != value for key, value in updates.items())
        if not changed:
            return state

        repaired = validated_copy(
            state,
            **updates,
            revision=state.revision + 1,
            updated_at=max(item.admitted_at for item in session_rows),
        )
        atomic_write_model(state_path, repaired)
        return repaired

    async def create_if_absent(
        self,
        record: InputAdmissionRecord,
    ) -> InputAdmissionRecord:
        async with self.locks.hold_identity_then_session(
            self.root,
            record.session_id,
        ):
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

            rows = await self.list_for_session(record.session_id)
            if any(item.session_sequence == record.session_sequence for item in rows):
                raise InputRuntimeConflictError(
                    "duplicate session admission sequence"
                )
            if any(
                item.target_cycle_id == record.target_cycle_id
                and item.cycle_sequence == record.cycle_sequence
                for item in rows
            ):
                raise InputRuntimeConflictError("duplicate cycle admission sequence")

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

    async def allocate(
        self,
        record: InputAdmissionRecord,
    ) -> InputAdmissionRecord:
        async with self.locks.hold_identity_then_session(
            self.root,
            record.session_id,
        ):
            state_path = self.layout.state(record.session_id)
            if not state_path.exists():
                raise InputRuntimeNotFoundError(
                    "session runtime state required for allocation"
                )
            state = read_model(state_path, SessionInputRuntimeState)
            state = self._repair_from_authoritative_admissions(state_path, state)

            existing = await self.get_by_input_batch_id(record.input_batch_id)
            if existing is not None:
                if not _same_admission_relation(existing, record):
                    raise InputRuntimeConflictError(
                        "input batch admission relation changed"
                    )
                state = self._repair_from_authoritative_admissions(
                    state_path,
                    state,
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
                raise InputRuntimeConflictError("admission stable ID collision")

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


class FileSystemCycleInboxRepository(_CycleInboxBase):
    """Inbox repository with root-global admission/input identity fencing."""

    async def create_if_absent(self, item: CycleInboxItem) -> CycleInboxItem:
        async with self.locks.hold_identity_then_session(
            self.root,
            item.session_id,
        ):
            for index_path in (
                self.layout.inbox_admission(item.admission_id),
                self.layout.inbox_input(item.input_batch_id),
            ):
                existing = self._from(index_path)
                if existing is not None:
                    if not _same_inbox_relation(existing, item):
                        raise InputRuntimeConflictError(
                            "inbox idempotency relation changed"
                        )
                    return existing

            matches = [
                row
                for row in self._scan()
                if row.admission_id == item.admission_id
                or row.input_batch_id == item.input_batch_id
            ]
            if matches:
                existing = matches[0]
                if len(matches) > 1 or not _same_inbox_relation(existing, item):
                    raise InputRuntimeConflictError(
                        "inbox admission/input identity conflict"
                    )
                self._index(existing)
                return existing

            try:
                by_id = await self._find_id(item.inbox_item_id)
            except InputRuntimeNotFoundError:
                by_id = None
            if by_id is not None:
                if by_id != item:
                    raise InputRuntimeConflictError("inbox stable ID collision")
                return by_id

            rows = await self.list_for_cycle(item.cycle_id)
            if any(row.cycle_sequence == item.cycle_sequence for row in rows):
                raise InputRuntimeConflictError(
                    "duplicate inbox cycle sequence"
                )
            self._ensure_cycle_authority(item.cycle_id, item.session_id)
            atomic_write_model(
                self.layout.inbox_item(item.cycle_id, item.inbox_item_id),
                item,
            )
            self._index(item)
            return item
