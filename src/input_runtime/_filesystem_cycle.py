"""Cycle inbox, snapshot, and context-revision filesystem repositories."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from ._filesystem_common import _RepositoryBase, validated_copy
from .errors import InputRuntimeConflictError, InputRuntimeNotFoundError
from .models import (
    ActiveCycleSnapshot,
    ClaimedInboxRange,
    CycleContextRevision,
    CycleInboxItem,
    CycleStatus,
    InboxState,
)
from .serialization import atomic_write_model, list_models, read_model


def _same_inbox_relation(
    existing: CycleInboxItem,
    incoming: CycleInboxItem,
) -> bool:
    """Return whether two records describe the same admitted batch in one cycle."""
    return (
        existing.admission_id,
        existing.session_id,
        existing.cycle_id,
        existing.input_batch_id,
        existing.generation,
        existing.payload_size_bytes,
    ) == (
        incoming.admission_id,
        incoming.session_id,
        incoming.cycle_id,
        incoming.input_batch_id,
        incoming.generation,
        incoming.payload_size_bytes,
    )


class FileSystemCycleInboxRepository(_RepositoryBase):
    def _index(self, record: CycleInboxItem) -> None:
        pointer = self._pointer(
            "inbox",
            record.inbox_item_id,
            record.session_id,
            self.layout.inbox_item(record.cycle_id, record.inbox_item_id),
            record.cycle_id,
        )
        self._write_pointer(
            self.layout.record_index("inbox", record.inbox_item_id),
            pointer,
        )
        self._write_pointer(
            self.layout.inbox_admission(record.admission_id),
            pointer,
        )
        self._write_pointer(
            self.layout.inbox_input(record.input_batch_id),
            pointer,
        )
        self._ensure_cycle_authority(record.cycle_id, record.session_id)

    def _from(self, path) -> CycleInboxItem | None:
        pointer = self._read_pointer(path)
        if pointer and self._pointer_record_path(pointer).exists():
            return read_model(
                self._pointer_record_path(pointer),
                CycleInboxItem,
            )
        return None

    def _scan(self) -> tuple[CycleInboxItem, ...]:
        directory = self.layout.root / "cycles"
        if not directory.exists():
            return ()
        return tuple(
            read_model(path, CycleInboxItem)
            for path in sorted(directory.glob("*/inbox/*.json"))
        )

    async def _find_id(self, record_id: str) -> CycleInboxItem:
        record = self._from(
            self.layout.record_index("inbox", record_id)
        )
        if record is not None:
            return record
        matches = [
            item for item in self._scan()
            if item.inbox_item_id == record_id
        ]
        if not matches:
            raise InputRuntimeNotFoundError(record_id)
        if len(matches) > 1:
            raise InputRuntimeConflictError("duplicate inbox stable ID")
        self._index(matches[0])
        return matches[0]

    async def create_if_absent(
        self,
        item: CycleInboxItem,
    ) -> CycleInboxItem:
        async with self.locks.hold(self.root, item.session_id):
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

            try:
                by_id = await self._find_id(item.inbox_item_id)
            except InputRuntimeNotFoundError:
                by_id = None
            if by_id is not None:
                if by_id != item:
                    raise InputRuntimeConflictError(
                        "inbox stable ID collision"
                    )
                return by_id

            records = await self.list_for_cycle(item.cycle_id)
            if any(
                existing.cycle_sequence == item.cycle_sequence
                for existing in records
            ):
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

    async def list_for_cycle(
        self,
        cycle_id: str,
    ) -> tuple[CycleInboxItem, ...]:
        records = list_models(
            self.layout.inbox(cycle_id),
            CycleInboxItem,
        )
        return tuple(
            sorted(records, key=lambda item: item.cycle_sequence)
        )

    async def claim_contiguous_range(
        self,
        cycle_id: str,
        *,
        generation: int,
        after_sequence: int,
        max_items: int,
        max_bytes: int,
        lease_seconds: int,
    ) -> ClaimedInboxRange | None:
        authority = self._read_pointer(
            self.layout.cycle_authority(cycle_id)
        )
        if authority is None:
            records = await self.list_for_cycle(cycle_id)
            if not records:
                return None
            session_id = records[0].session_id
            self._ensure_cycle_authority(cycle_id, session_id)
        else:
            session_id = authority.session_id

        async with self.locks.hold(self.root, session_id):
            records = [
                item
                for item in await self.list_for_cycle(cycle_id)
                if item.generation == generation
                and item.cycle_sequence > after_sequence
                and item.state == InboxState.QUEUED
            ]
            records.sort(key=lambda item: item.cycle_sequence)
            if not records or records[0].cycle_sequence != after_sequence + 1:
                return None

            selected: list[CycleInboxItem] = []
            total_bytes = 0
            expected = after_sequence + 1
            for item in records:
                if item.cycle_sequence != expected or len(selected) >= max_items:
                    break
                size = item.payload_size_bytes
                if size > max_bytes and not selected:
                    return None
                if selected and total_bytes + size > max_bytes:
                    break
                selected.append(item)
                total_bytes += size
                expected += 1
            if not selected:
                return None

            claimed_at = datetime.now(timezone.utc)
            expires_at = claimed_at + timedelta(seconds=lease_seconds)
            token = uuid4().hex
            claimed = []
            for stale in selected:
                path = self.layout.inbox_item(
                    cycle_id,
                    stale.inbox_item_id,
                )
                current = read_model(path, CycleInboxItem)
                if current.state != InboxState.QUEUED:
                    raise InputRuntimeConflictError(
                        "inbox changed before claim"
                    )
                updated = validated_copy(
                    current,
                    state=InboxState.CLAIMED,
                    claim_token=token,
                    claimed_at=claimed_at,
                    claim_expires_at=expires_at,
                    attempt_count=current.attempt_count + 1,
                )
                atomic_write_model(path, updated)
                claimed.append(updated)

            return ClaimedInboxRange(
                cycle_id=cycle_id,
                generation=generation,
                claim_token=token,
                first_cycle_sequence=claimed[0].cycle_sequence,
                last_cycle_sequence=claimed[-1].cycle_sequence,
                items=tuple(claimed),
                claimed_bytes=total_bytes,
                claim_expires_at=expires_at,
            )

    async def _transition(
        self,
        claim: ClaimedInboxRange,
        required_states: set[InboxState],
        next_state: InboxState,
        *,
        applied_at: datetime | None = None,
        error_code: str | None = None,
    ) -> tuple[CycleInboxItem, ...]:
        async with self.locks.hold(
            self.root,
            claim.items[0].session_id,
        ):
            current_items = []
            for claimed in claim.items:
                path = self.layout.inbox_item(
                    claim.cycle_id,
                    claimed.inbox_item_id,
                )
                current = read_model(path, CycleInboxItem)
                if (
                    current.state not in required_states
                    or current.claim_token != claim.claim_token
                    or current.generation != claim.generation
                ):
                    raise InputRuntimeConflictError("stale inbox claim")
                current_items.append((path, current))

            updated_items = []
            for path, current in current_items:
                updates: dict[str, object] = {"state": next_state}
                if next_state == InboxState.APPLIED:
                    updates.update(
                        applied_at=applied_at,
                        claim_token=None,
                        claimed_at=None,
                        claim_expires_at=None,
                        last_error_code=None,
                    )
                elif next_state == InboxState.QUEUED:
                    updates.update(
                        claim_token=None,
                        claimed_at=None,
                        claim_expires_at=None,
                        last_error_code=error_code,
                    )
                updated = validated_copy(current, **updates)
                atomic_write_model(path, updated)
                updated_items.append(updated)
            return tuple(updated_items)

    async def mark_applying(
        self,
        claim: ClaimedInboxRange,
    ) -> ClaimedInboxRange:
        items = await self._transition(
            claim,
            {InboxState.CLAIMED},
            InboxState.APPLYING,
        )
        return validated_copy(claim, items=items)

    async def mark_applied(
        self,
        claim: ClaimedInboxRange,
        *,
        applied_at: datetime,
    ) -> tuple[CycleInboxItem, ...]:
        return await self._transition(
            claim,
            {InboxState.CLAIMED, InboxState.APPLYING},
            InboxState.APPLIED,
            applied_at=applied_at,
        )

    async def requeue_claim(
        self,
        claim: ClaimedInboxRange,
        *,
        error_code: str | None = None,
    ) -> tuple[CycleInboxItem, ...]:
        return await self._transition(
            claim,
            {InboxState.CLAIMED, InboxState.APPLYING},
            InboxState.QUEUED,
            error_code=error_code,
        )

    async def recover_expired_claims(
        self,
        *,
        now: datetime,
    ) -> tuple[CycleInboxItem, ...]:
        changed = []
        for stale in self._scan():
            if (
                stale.state not in {InboxState.CLAIMED, InboxState.APPLYING}
                or stale.claim_expires_at is None
                or stale.claim_expires_at > now
            ):
                continue
            async with self.locks.hold(self.root, stale.session_id):
                path = self.layout.inbox_item(
                    stale.cycle_id,
                    stale.inbox_item_id,
                )
                current = read_model(path, CycleInboxItem)
                if (
                    current.state not in {
                        InboxState.CLAIMED,
                        InboxState.APPLYING,
                    }
                    or current.claim_expires_at is None
                    or current.claim_expires_at > now
                ):
                    continue

                applied = False
                snapshot_path = self.layout.snapshot(current.cycle_id)
                if (
                    current.state == InboxState.APPLYING
                    and snapshot_path.exists()
                ):
                    snapshot = read_model(
                        snapshot_path,
                        ActiveCycleSnapshot,
                    )
                    applied = (
                        snapshot.generation == current.generation
                        and snapshot.applied_through_cycle_sequence
                        >= current.cycle_sequence
                        and current.input_batch_id
                        in snapshot.applied_input_batch_ids
                    )

                if applied:
                    updated = validated_copy(
                        current,
                        state=InboxState.APPLIED,
                        applied_at=now,
                        claim_token=None,
                        claimed_at=None,
                        claim_expires_at=None,
                        last_error_code=None,
                    )
                else:
                    updated = validated_copy(
                        current,
                        state=InboxState.QUEUED,
                        claim_token=None,
                        claimed_at=None,
                        claim_expires_at=None,
                        last_error_code="claim_expired",
                    )
                atomic_write_model(path, updated)
                changed.append(updated)
        return tuple(changed)

    async def cancel_generation(
        self,
        session_id: str,
        *,
        generation: int,
        cancelled_at: datetime,
        reason_code: str,
    ) -> tuple[CycleInboxItem, ...]:
        changed = []
        async with self.locks.hold(self.root, session_id):
            for stale in self._scan():
                if stale.session_id != session_id:
                    continue
                path = self.layout.inbox_item(
                    stale.cycle_id,
                    stale.inbox_item_id,
                )
                current = read_model(path, CycleInboxItem)
                if (
                    current.generation == generation
                    and current.state
                    in {
                        InboxState.QUEUED,
                        InboxState.CLAIMED,
                        InboxState.APPLYING,
                    }
                ):
                    updated = validated_copy(
                        current,
                        state=InboxState.CANCELLED,
                        cancelled_at=cancelled_at,
                        claim_token=None,
                        claimed_at=None,
                        claim_expires_at=None,
                        last_error_code=reason_code,
                    )
                    atomic_write_model(path, updated)
                    changed.append(updated)
        return tuple(changed)


class FileSystemActiveCycleSnapshotRepository(_RepositoryBase):
    def _index(self, record: ActiveCycleSnapshot) -> None:
        pointer = self._pointer(
            "snapshot",
            record.cycle_id,
            record.session_id,
            self.layout.snapshot(record.cycle_id),
            record.cycle_id,
        )
        self._write_pointer(
            self.layout.record_index("snapshot", record.cycle_id),
            pointer,
        )
        self._ensure_cycle_authority(record.cycle_id, record.session_id)

    async def create_if_absent(
        self,
        snapshot: ActiveCycleSnapshot,
    ) -> ActiveCycleSnapshot:
        async with self.locks.hold(self.root, snapshot.session_id):
            path = self.layout.snapshot(snapshot.cycle_id)
            if path.exists():
                current = read_model(path, ActiveCycleSnapshot)
                if current != snapshot:
                    raise InputRuntimeConflictError(
                        "snapshot stable ID collision"
                    )
                return current
            self._ensure_cycle_authority(
                snapshot.cycle_id,
                snapshot.session_id,
            )
            atomic_write_model(path, snapshot)
            self._index(snapshot)
            return snapshot

    async def get(self, cycle_id: str) -> ActiveCycleSnapshot | None:
        path = self.layout.snapshot(cycle_id)
        return read_model(path, ActiveCycleSnapshot) if path.exists() else None

    async def compare_and_swap(
        self,
        expected_revision: int,
        snapshot: ActiveCycleSnapshot,
    ) -> ActiveCycleSnapshot:
        async with self.locks.hold(self.root, snapshot.session_id):
            path = self.layout.snapshot(snapshot.cycle_id)
            if not path.exists():
                raise InputRuntimeNotFoundError(snapshot.cycle_id)
            current = read_model(path, ActiveCycleSnapshot)
            if current.snapshot_revision != expected_revision:
                raise InputRuntimeConflictError("stale snapshot revision")
            if snapshot.snapshot_revision != expected_revision + 1:
                raise InputRuntimeConflictError(
                    "snapshot revision must advance by one"
                )
            if (
                current.session_id,
                current.cycle_id,
                current.generation,
            ) != (
                snapshot.session_id,
                snapshot.cycle_id,
                snapshot.generation,
            ):
                raise InputRuntimeConflictError("snapshot identity changed")
            atomic_write_model(path, snapshot)
            return snapshot

    async def _all(self) -> tuple[ActiveCycleSnapshot, ...]:
        directory = self.layout.root / "cycles"
        if not directory.exists():
            return ()
        return tuple(
            read_model(path, ActiveCycleSnapshot)
            for path in sorted(directory.glob("*/snapshot.json"))
        )

    async def list_active(self) -> tuple[ActiveCycleSnapshot, ...]:
        terminal = {
            CycleStatus.DONE,
            CycleStatus.ERROR,
            CycleStatus.CANCELLED,
        }
        return tuple(
            item for item in await self._all()
            if item.status not in terminal
        )

    async def list_resumable(self) -> tuple[ActiveCycleSnapshot, ...]:
        resumable = {
            CycleStatus.RUNNING,
            CycleStatus.WAITING_USER,
            CycleStatus.PAUSE_REQUESTED,
            CycleStatus.PAUSED_BY_USER,
            CycleStatus.INTERRUPTED,
            CycleStatus.FINALIZING,
        }
        return tuple(
            item for item in await self._all()
            if item.status in resumable
        )

    async def cancel_generation(
        self,
        session_id: str,
        *,
        generation: int,
        reason_code: str,
    ) -> tuple[ActiveCycleSnapshot, ...]:
        changed = []
        resumable = {
            CycleStatus.RUNNING,
            CycleStatus.WAITING_USER,
            CycleStatus.PAUSE_REQUESTED,
            CycleStatus.PAUSED_BY_USER,
            CycleStatus.INTERRUPTED,
            CycleStatus.FINALIZING,
        }
        async with self.locks.hold(self.root, session_id):
            for stale in await self._all():
                if stale.session_id != session_id:
                    continue
                path = self.layout.snapshot(stale.cycle_id)
                current = read_model(path, ActiveCycleSnapshot)
                if (
                    current.generation == generation
                    and current.status in resumable
                ):
                    updated = validated_copy(
                        current,
                        status=CycleStatus.CANCELLED,
                        cancellation_reason_code=reason_code,
                        waiting_question=None,
                        pause_reason=None,
                        interruption_reason=None,
                        snapshot_revision=current.snapshot_revision + 1,
                        updated_at=datetime.now(timezone.utc),
                    )
                    atomic_write_model(path, updated)
                    changed.append(updated)
        return tuple(changed)


class FileSystemContextRevisionRepository(_RepositoryBase):
    def _index(self, record: CycleContextRevision) -> None:
        pointer = self._pointer(
            "revision",
            record.context_revision_id,
            record.session_id,
            self.layout.revision(
                record.cycle_id,
                record.context_revision_id,
            ),
            record.cycle_id,
        )
        self._write_pointer(
            self.layout.record_index(
                "revision",
                record.context_revision_id,
            ),
            pointer,
        )
        self._ensure_cycle_authority(record.cycle_id, record.session_id)

    async def append_revision(
        self,
        revision: CycleContextRevision,
    ) -> CycleContextRevision:
        async with self.locks.hold(self.root, revision.session_id):
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
                return current

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

            self._ensure_cycle_authority(
                revision.cycle_id,
                revision.session_id,
            )
            atomic_write_model(path, revision)
            self._index(revision)
            return revision

    async def get(
        self,
        context_revision_id: str,
    ) -> CycleContextRevision | None:
        pointer = self._read_pointer(
            self.layout.record_index("revision", context_revision_id)
        )
        if pointer and self._pointer_record_path(pointer).exists():
            return read_model(
                self._pointer_record_path(pointer),
                CycleContextRevision,
            )

        directory = self.layout.root / "cycles"
        records = []
        if directory.exists():
            for path in sorted(
                directory.glob("*/context-revisions/*.json")
            ):
                record = read_model(path, CycleContextRevision)
                if record.context_revision_id == context_revision_id:
                    records.append(record)
        if len(records) > 1:
            raise InputRuntimeConflictError(
                "duplicate context revision ID"
            )
        if records:
            self._index(records[0])
            return records[0]
        return None

    async def get_latest(
        self,
        cycle_id: str,
    ) -> CycleContextRevision | None:
        records = await self.list_for_cycle(cycle_id)
        return records[-1] if records else None

    async def list_for_cycle(
        self,
        cycle_id: str,
    ) -> tuple[CycleContextRevision, ...]:
        records = list_models(
            self.layout.revisions(cycle_id),
            CycleContextRevision,
        )
        return tuple(
            sorted(records, key=lambda item: item.revision_number)
        )
