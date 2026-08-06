"""Globally fenced filesystem adapters for durable input-runtime identities."""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel

from . import _filesystem_session as session_repository_module
from ._filesystem_common import _RepositoryBase, validated_copy
from ._filesystem_cycle import (
    FileSystemActiveCycleSnapshotRepository as _SnapshotBase,
    FileSystemContextRevisionRepository as _ContextRevisionBase,
    FileSystemCycleInboxRepository as _CycleInboxBase,
    _same_inbox_relation,
)
from ._filesystem_delivery import (
    FileSystemAgentEmissionRepository as _EmissionBase,
    FileSystemFinalizationRepository as _FinalizationBase,
    _final_identity,
    _same_emission_relation,
)
from ._filesystem_session import (
    FileSystemInputAdmissionRepository as _AdmissionBase,
    FileSystemSessionControlRepository as _ControlBase,
    TERMINAL_OR_IDLE,
    _same_admission_relation,
    _same_control_relation,
)
from .errors import InputRuntimeConflictError, InputRuntimeNotFoundError
from .models import (
    ActiveCycleSnapshot,
    AdmissionKind,
    AgentEmission,
    CycleContextRevision,
    CycleFinalizationRecord,
    CycleInboxItem,
    CycleStatus,
    FinalizationState,
    InputAdmissionRecord,
    SessionControlCommand,
    SessionInputRuntimeState,
)
from .serialization import list_models, read_model


ModelT = TypeVar("ModelT", bound=BaseModel)


def atomic_write_model(path, model):
    """Forward writes through the canonical session-module test seam."""
    return session_repository_module.atomic_write_model(path, model)


def _read_indexed(
    repository: _RepositoryBase,
    index_path,
    model_type: type[ModelT],
    *,
    identity_name: str,
) -> ModelT | None:
    pointer = repository._read_pointer(index_path)
    if pointer is None:
        return None
    record_path = repository._pointer_record_path(pointer)
    if not record_path.exists():
        raise InputRuntimeConflictError(
            f"incomplete {identity_name} identity reservation"
        )
    return read_model(record_path, model_type)


class FileSystemInputAdmissionRepository(_AdmissionBase):
    """Admission repository with root-global identity fencing and repair."""

    def _indexed_by_input_batch_id(
        self,
        input_batch_id: str,
    ) -> InputAdmissionRecord | None:
        return _read_indexed(
            self,
            self.layout.admission_input(input_batch_id),
            InputAdmissionRecord,
            identity_name="input batch admission",
        )

    def _indexed_by_admission_id(
        self,
        admission_id: str,
    ) -> InputAdmissionRecord | None:
        return _read_indexed(
            self,
            self.layout.record_index("admission", admission_id),
            InputAdmissionRecord,
            identity_name="admission",
        )

    async def _repair_from_authoritative_admissions(
        self,
        state_path,
        state: SessionInputRuntimeState,
    ) -> SessionInputRuntimeState:
        session_rows = list(await self.list_for_session(state.session_id))
        if not session_rows:
            if state.accepted_through_session_sequence != 0:
                raise InputRuntimeConflictError(
                    "session watermark has no authoritative admissions"
                )
            return state

        session_rows.sort(key=lambda item: item.session_sequence)
        sequences = [item.session_sequence for item in session_rows]
        if len(sequences) != len(set(sequences)):
            raise InputRuntimeConflictError(
                "duplicate authoritative session sequence"
            )
        expected_session_sequences = list(range(1, sequences[-1] + 1))
        if sequences != expected_session_sequences:
            raise InputRuntimeConflictError(
                "gap in authoritative session sequence"
            )
        if state.accepted_through_session_sequence > sequences[-1]:
            raise InputRuntimeConflictError(
                "session watermark exceeds authoritative admissions"
            )

        cycle_groups: dict[tuple[int, str], list[InputAdmissionRecord]] = {}
        for item in session_rows:
            cycle_groups.setdefault(
                (item.admitted_generation, item.target_cycle_id),
                [],
            ).append(item)
        for rows in cycle_groups.values():
            cycle_sequences = sorted(item.cycle_sequence for item in rows)
            if len(cycle_sequences) != len(set(cycle_sequences)):
                raise InputRuntimeConflictError(
                    "duplicate authoritative active-cycle sequence"
                )
            expected_cycle_sequences = list(
                range(0, cycle_sequences[-1] + 1)
            )
            if cycle_sequences != expected_cycle_sequences:
                raise InputRuntimeConflictError(
                    "gap in authoritative active-cycle sequence"
                )
            starts = [
                item
                for item in rows
                if item.admission_kind == AdmissionKind.START_CYCLE
            ]
            if len(starts) != 1 or starts[0].cycle_sequence != 0:
                raise InputRuntimeConflictError(
                    "authoritative cycle is missing a unique start admission"
                )

        accepted_session = sequences[-1]
        generation_rows = [
            item
            for item in session_rows
            if item.admitted_generation == state.generation
        ]
        orphan_starts = [
            item
            for item in generation_rows
            if item.admission_kind == AdmissionKind.START_CYCLE
            and item.session_sequence
            > state.accepted_through_session_sequence
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
        if active_cycle_id is not None and not active_rows:
            raise InputRuntimeConflictError(
                "active cycle has no authoritative admissions"
            )
        active_accepted = max(
            (item.cycle_sequence for item in active_rows),
            default=0,
        )
        if (
            state.active_cycle_id == active_cycle_id
            and state.active_cycle_accepted_through_sequence
            > active_accepted
        ):
            raise InputRuntimeConflictError(
                "active-cycle watermark exceeds authoritative admissions"
            )
        if active_applied > active_accepted:
            raise InputRuntimeConflictError(
                "applied active-cycle watermark exceeds authoritative admissions"
            )

        latest_admitted_at = max(item.admitted_at for item in session_rows)
        updates = {
            "accepted_through_session_sequence": accepted_session,
            "active_cycle_id": active_cycle_id,
            "cycle_status": cycle_status,
            "active_cycle_accepted_through_sequence": active_accepted,
            "active_cycle_applied_through_sequence": active_applied,
            "active_context_revision_id": active_context_revision_id,
            "finalization_id": finalization_id,
            "updated_at": max(state.updated_at, latest_admitted_at),
        }
        changed = any(
            getattr(state, key) != value
            for key, value in updates.items()
        )
        if not changed:
            return state

        repaired = validated_copy(
            state,
            **updates,
            revision=state.revision + 1,
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
            existing = self._indexed_by_input_batch_id(record.input_batch_id)
            if existing is not None:
                if not _same_admission_relation(existing, record):
                    raise InputRuntimeConflictError(
                        "input batch admission relation changed"
                    )
                return existing

            by_id = self._indexed_by_admission_id(record.admission_id)
            if by_id is not None:
                if by_id != record:
                    raise InputRuntimeConflictError(
                        "admission stable ID collision"
                    )
                return by_id

            rows = await self.list_for_session(record.session_id)
            if any(
                item.session_sequence == record.session_sequence
                for item in rows
            ):
                raise InputRuntimeConflictError(
                    "duplicate session admission sequence"
                )
            if any(
                item.target_cycle_id == record.target_cycle_id
                and item.cycle_sequence == record.cycle_sequence
                for item in rows
            ):
                raise InputRuntimeConflictError(
                    "duplicate cycle admission sequence"
                )

            self._ensure_cycle_authority(
                record.target_cycle_id,
                record.session_id,
            )
            self._index_record(record)
            atomic_write_model(
                self.layout.admission(
                    record.session_id,
                    record.admission_id,
                ),
                record,
            )
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
            state = await self._repair_from_authoritative_admissions(
                state_path,
                state,
            )

            existing = self._indexed_by_input_batch_id(record.input_batch_id)
            if existing is not None:
                if not _same_admission_relation(existing, record):
                    raise InputRuntimeConflictError(
                        "input batch admission relation changed"
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
            if next_state.updated_at < state.updated_at:
                next_state = validated_copy(
                    next_state,
                    updated_at=state.updated_at,
                )

            by_id = self._indexed_by_admission_id(
                allocated.admission_id
            )
            if by_id is not None and by_id != allocated:
                raise InputRuntimeConflictError(
                    "admission stable ID collision"
                )

            self._ensure_cycle_authority(
                allocated.target_cycle_id,
                allocated.session_id,
            )
            self._index_record(allocated)
            atomic_write_model(
                self.layout.admission(
                    allocated.session_id,
                    allocated.admission_id,
                ),
                allocated,
            )
            atomic_write_model(state_path, next_state)
            return allocated


class FileSystemCycleInboxRepository(_CycleInboxBase):
    """Inbox repository with root-global admission/input identity fencing."""

    async def create_if_absent(
        self,
        item: CycleInboxItem,
    ) -> CycleInboxItem:
        async with self.locks.hold_identity_then_session(
            self.root,
            item.session_id,
        ):
            existing_records = []
            for index_path, identity_name in (
                (
                    self.layout.inbox_admission(item.admission_id),
                    "inbox admission",
                ),
                (
                    self.layout.inbox_input(item.input_batch_id),
                    "inbox input",
                ),
            ):
                existing = _read_indexed(
                    self,
                    index_path,
                    CycleInboxItem,
                    identity_name=identity_name,
                )
                if existing is not None:
                    existing_records.append(existing)
            if existing_records:
                first = existing_records[0]
                if (
                    any(record != first for record in existing_records[1:])
                    or not _same_inbox_relation(first, item)
                ):
                    raise InputRuntimeConflictError(
                        "inbox admission/input identity conflict"
                    )
                return first

            by_id = _read_indexed(
                self,
                self.layout.record_index("inbox", item.inbox_item_id),
                CycleInboxItem,
                identity_name="inbox",
            )
            if by_id is not None:
                if by_id != item:
                    raise InputRuntimeConflictError(
                        "inbox stable ID collision"
                    )
                return by_id

            rows = await self.list_for_cycle(item.cycle_id)
            if any(
                row.cycle_sequence == item.cycle_sequence
                for row in rows
            ):
                raise InputRuntimeConflictError(
                    "duplicate inbox cycle sequence"
                )
            self._ensure_cycle_authority(
                item.cycle_id,
                item.session_id,
            )
            self._index(item)
            atomic_write_model(
                self.layout.inbox_item(
                    item.cycle_id,
                    item.inbox_item_id,
                ),
                item,
            )
            return item


class FileSystemSessionControlRepository(_ControlBase):
    """Control repository with globally fenced stable and cycle identities."""

    async def append(
        self,
        command: SessionControlCommand,
    ) -> SessionControlCommand:
        async with self.locks.hold_identity_then_session(
            self.root,
            command.session_id,
        ):
            existing = await self.get_by_idempotency_key(
                command.session_id,
                command.idempotency_key,
            )
            if existing is not None:
                if not _same_control_relation(existing, command):
                    raise InputRuntimeConflictError(
                        "control idempotency relation changed"
                    )
                return existing

            by_id = _read_indexed(
                self,
                self.layout.record_index("control", command.control_id),
                SessionControlCommand,
                identity_name="control",
            )
            if by_id is not None:
                if by_id != command:
                    raise InputRuntimeConflictError(
                        "control stable ID collision"
                    )
                return by_id

            if any(
                item.sequence_number == command.sequence_number
                for item in await self._all(command.session_id)
            ):
                raise InputRuntimeConflictError(
                    "duplicate control sequence"
                )
            if command.target_cycle_id is not None:
                self._ensure_cycle_authority(
                    command.target_cycle_id,
                    command.session_id,
                )
            self._index(command)
            atomic_write_model(
                self.layout.control(
                    command.session_id,
                    command.control_id,
                ),
                command,
            )
            return command


class FileSystemActiveCycleSnapshotRepository(_SnapshotBase):
    """Snapshot repository with globally fenced cycle authority."""

    async def create_if_absent(
        self,
        snapshot: ActiveCycleSnapshot,
    ) -> ActiveCycleSnapshot:
        async with self.locks.hold_identity_then_session(
            self.root,
            snapshot.session_id,
        ):
            path = self.layout.snapshot(snapshot.cycle_id)
            if path.exists():
                current = read_model(path, ActiveCycleSnapshot)
                if current != snapshot:
                    raise InputRuntimeConflictError(
                        "snapshot stable ID collision"
                    )
                self._index(current)
                return current

            indexed = _read_indexed(
                self,
                self.layout.record_index("snapshot", snapshot.cycle_id),
                ActiveCycleSnapshot,
                identity_name="snapshot",
            )
            if indexed is not None:
                if indexed != snapshot:
                    raise InputRuntimeConflictError(
                        "snapshot stable ID collision"
                    )
                return indexed

            self._ensure_cycle_authority(
                snapshot.cycle_id,
                snapshot.session_id,
            )
            self._index(snapshot)
            atomic_write_model(path, snapshot)
            return snapshot


class FileSystemContextRevisionRepository(_ContextRevisionBase):
    """Context revisions with globally fenced stable and cycle identities."""

    async def append_revision(
        self,
        revision: CycleContextRevision,
    ) -> CycleContextRevision:
        async with self.locks.hold_identity_then_session(
            self.root,
            revision.session_id,
        ):
            by_id = _read_indexed(
                self,
                self.layout.record_index(
                    "revision",
                    revision.context_revision_id,
                ),
                CycleContextRevision,
                identity_name="context revision",
            )
            if by_id is not None:
                if by_id != revision:
                    raise InputRuntimeConflictError(
                        "context revision stable ID collision"
                    )
                return by_id

            path = self.layout.revision(
                revision.cycle_id,
                revision.context_revision_id,
            )
            if path.exists():
                current = read_model(path, CycleContextRevision)
                if current != revision:
                    raise InputRuntimeConflictError(
                        "context revision stable ID collision"
                    )
                self._index(current)
                return current

            self._ensure_cycle_authority(
                revision.cycle_id,
                revision.session_id,
            )
            latest = await self.get_latest(revision.cycle_id)
            if latest is None and revision.revision_number != 1:
                raise InputRuntimeConflictError(
                    "first revision must be 1"
                )
            if latest is not None:
                if revision.revision_number != latest.revision_number + 1:
                    raise InputRuntimeConflictError(
                        "context revision sequence gap"
                    )
                if revision.parent_revision_ids != [
                    latest.context_revision_id
                ]:
                    raise InputRuntimeConflictError(
                        "context revision parent mismatch"
                    )

            self._index(revision)
            atomic_write_model(path, revision)
            return revision


class FileSystemAgentEmissionRepository(_EmissionBase):
    """Agent emissions with globally fenced stable and cycle identities."""

    async def get_by_idempotency_key(
        self,
        cycle_id: str,
        idempotency_key: str,
    ) -> AgentEmission | None:
        return next(
            (
                item
                for item in list_models(
                    self.layout.emissions(cycle_id),
                    AgentEmission,
                )
                if item.idempotency_key == idempotency_key.strip()
            ),
            None,
        )

    async def create_if_absent(
        self,
        emission: AgentEmission,
    ) -> AgentEmission:
        async with self.locks.hold_identity_then_session(
            self.root,
            emission.session_id,
        ):
            existing = await self.get_by_idempotency_key(
                emission.cycle_id,
                emission.idempotency_key,
            )
            if existing is not None:
                if not _same_emission_relation(existing, emission):
                    raise InputRuntimeConflictError(
                        "emission idempotency relation changed"
                    )
                return existing

            by_id = _read_indexed(
                self,
                self.layout.record_index(
                    "emission",
                    emission.emission_id,
                ),
                AgentEmission,
                identity_name="emission",
            )
            if by_id is not None:
                if by_id != emission:
                    raise InputRuntimeConflictError(
                        "emission stable ID collision"
                    )
                return by_id

            path = self.layout.emission(
                emission.cycle_id,
                emission.emission_id,
            )
            if path.exists():
                current = read_model(path, AgentEmission)
                if current != emission:
                    raise InputRuntimeConflictError(
                        "emission stable ID collision"
                    )
                self._index(current)
                return current

            self._ensure_cycle_authority(
                emission.cycle_id,
                emission.session_id,
            )
            self._index(emission)
            atomic_write_model(path, emission)
            return emission


class FileSystemFinalizationRepository(_FinalizationBase):
    """Finalizations with globally fenced stable and cycle identities."""

    async def prepare(
        self,
        record: CycleFinalizationRecord,
    ) -> CycleFinalizationRecord:
        if record.state != FinalizationState.PREPARED:
            raise ValueError("prepare requires PREPARED")
        async with self.locks.hold_identity_then_session(
            self.root,
            record.session_id,
        ):
            by_id = _read_indexed(
                self,
                self.layout.record_index(
                    "finalization",
                    record.finalization_id,
                ),
                CycleFinalizationRecord,
                identity_name="finalization",
            )
            if by_id is not None:
                if by_id != record:
                    raise InputRuntimeConflictError(
                        "finalization stable ID collision"
                    )
                return by_id

            path = self.layout.finalization(
                record.cycle_id,
                record.finalization_id,
            )
            if path.exists():
                current = read_model(path, CycleFinalizationRecord)
                if _final_identity(current) != _final_identity(record):
                    raise InputRuntimeConflictError(
                        "finalization stable ID collision"
                    )
                if current != record:
                    raise InputRuntimeConflictError(
                        "finalization state already advanced"
                    )
                self._index(current)
                return current

            self._ensure_cycle_authority(
                record.cycle_id,
                record.session_id,
            )
            self._index(record)
            atomic_write_model(path, record)
            return record
