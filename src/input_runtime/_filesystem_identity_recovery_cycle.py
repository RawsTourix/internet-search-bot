"""Crash-recoverable inbox, snapshot, and context repositories."""

from __future__ import annotations

from . import _filesystem_identity as identity_module
from ._filesystem_cycle import _same_inbox_relation
from ._filesystem_identity import (
    FileSystemActiveCycleSnapshotRepository as _SnapshotIdentityBase,
    FileSystemContextRevisionRepository as _ContextIdentityBase,
    FileSystemCycleInboxRepository as _InboxIdentityBase,
)
from ._filesystem_identity_recovery_common import (
    recover_cycle_authority,
    recover_indexed,
    scan_models,
)
from .errors import InputRuntimeConflictError
from .models import (
    ActiveCycleSnapshot,
    CycleContextRevision,
    CycleInboxItem,
)


def atomic_write_model(path, model):
    """Keep the existing identity-module write seam used by crash tests."""
    return identity_module.atomic_write_model(path, model)


class FileSystemCycleInboxRepository(_InboxIdentityBase):
    """Inbox writes with recoverable stable and relation indexes."""

    def _scan_all(self) -> tuple[CycleInboxItem, ...]:
        return scan_models(
            self.layout.root.glob("cycles/*/inbox/*.json"),
            CycleInboxItem,
            identity_name="inbox",
        )

    def _restore_indexes(self, item: CycleInboxItem) -> None:
        recover_cycle_authority(self, item.cycle_id, item.session_id)
        self._index(item)

    async def create_if_absent(
        self,
        item: CycleInboxItem,
    ) -> CycleInboxItem:
        async with self.locks.hold_identity_then_session(
            self.root,
            item.session_id,
        ):
            cached_rows: tuple[CycleInboxItem, ...] | None = None

            def scan() -> tuple[CycleInboxItem, ...]:
                nonlocal cached_rows
                if cached_rows is None:
                    cached_rows = self._scan_all()
                return cached_rows

            identities = (
                (
                    self.layout.inbox_admission(item.admission_id),
                    "inbox admission",
                    lambda record: record.admission_id == item.admission_id,
                ),
                (
                    self.layout.inbox_input(item.input_batch_id),
                    "inbox input",
                    lambda record: record.input_batch_id == item.input_batch_id,
                ),
                (
                    self.layout.record_index("inbox", item.inbox_item_id),
                    "inbox",
                    lambda record: record.inbox_item_id == item.inbox_item_id,
                ),
            )
            existing_records = []
            for index_path, identity_name, matcher in identities:
                existing = recover_indexed(
                    self,
                    index_path,
                    CycleInboxItem,
                    identity_name=identity_name,
                    matches_identity=matcher,
                    scan=scan,
                    restore=self._restore_indexes,
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
                self._restore_indexes(first)
                return first

            rows = await self.list_for_cycle(item.cycle_id)
            if any(
                row.cycle_sequence == item.cycle_sequence
                for row in rows
            ):
                raise InputRuntimeConflictError(
                    "duplicate inbox cycle sequence"
                )
            recover_cycle_authority(
                self,
                item.cycle_id,
                item.session_id,
            )
            atomic_write_model(
                self.layout.inbox_item(
                    item.cycle_id,
                    item.inbox_item_id,
                ),
                item,
            )
            self._index(item)
            return item


class FileSystemActiveCycleSnapshotRepository(_SnapshotIdentityBase):
    """Snapshot writes with recoverable cycle ownership."""

    def _scan_all(self) -> tuple[ActiveCycleSnapshot, ...]:
        return scan_models(
            self.layout.root.glob("cycles/*/snapshot.json"),
            ActiveCycleSnapshot,
            identity_name="snapshot",
        )

    def _restore_indexes(self, snapshot: ActiveCycleSnapshot) -> None:
        recover_cycle_authority(
            self,
            snapshot.cycle_id,
            snapshot.session_id,
        )
        self._index(snapshot)

    async def create_if_absent(
        self,
        snapshot: ActiveCycleSnapshot,
    ) -> ActiveCycleSnapshot:
        async with self.locks.hold_identity_then_session(
            self.root,
            snapshot.session_id,
        ):
            existing = recover_indexed(
                self,
                self.layout.record_index("snapshot", snapshot.cycle_id),
                ActiveCycleSnapshot,
                identity_name="snapshot",
                matches_identity=lambda item: item.cycle_id == snapshot.cycle_id,
                scan=self._scan_all,
                restore=self._restore_indexes,
            )
            if existing is not None:
                if existing != snapshot:
                    raise InputRuntimeConflictError(
                        "snapshot stable ID collision"
                    )
                self._restore_indexes(existing)
                return existing

            recover_cycle_authority(
                self,
                snapshot.cycle_id,
                snapshot.session_id,
            )
            atomic_write_model(
                self.layout.snapshot(snapshot.cycle_id),
                snapshot,
            )
            self._index(snapshot)
            return snapshot


class FileSystemContextRevisionRepository(_ContextIdentityBase):
    """Context writes with recoverable stable and cycle indexes."""

    def _scan_all(self) -> tuple[CycleContextRevision, ...]:
        return scan_models(
            self.layout.root.glob("cycles/*/context-revisions/*.json"),
            CycleContextRevision,
            identity_name="context revision",
        )

    def _restore_indexes(self, revision: CycleContextRevision) -> None:
        recover_cycle_authority(
            self,
            revision.cycle_id,
            revision.session_id,
        )
        self._index(revision)

    async def append_revision(
        self,
        revision: CycleContextRevision,
    ) -> CycleContextRevision:
        async with self.locks.hold_identity_then_session(
            self.root,
            revision.session_id,
        ):
            existing = recover_indexed(
                self,
                self.layout.record_index(
                    "revision",
                    revision.context_revision_id,
                ),
                CycleContextRevision,
                identity_name="context revision",
                matches_identity=lambda item: (
                    item.context_revision_id == revision.context_revision_id
                ),
                scan=self._scan_all,
                restore=self._restore_indexes,
            )
            if existing is not None:
                if existing != revision:
                    raise InputRuntimeConflictError(
                        "context revision stable ID collision"
                    )
                self._restore_indexes(existing)
                return existing

            recover_cycle_authority(
                self,
                revision.cycle_id,
                revision.session_id,
            )
            latest = await self.get_latest(revision.cycle_id)
            if latest is None and revision.revision_number != 1:
                raise InputRuntimeConflictError("first revision must be 1")
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

            atomic_write_model(
                self.layout.revision(
                    revision.cycle_id,
                    revision.context_revision_id,
                ),
                revision,
            )
            self._index(revision)
            return revision
