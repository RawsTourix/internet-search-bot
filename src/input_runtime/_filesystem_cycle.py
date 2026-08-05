"""Cycle inbox, snapshot, and context-revision filesystem repositories."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .errors import InputRuntimeConflictError, InputRuntimeNotFoundError
from .models import (
    ActiveCycleSnapshot, ClaimedInboxRange, CycleContextRevision,
    CycleInboxItem, CycleStatus, InboxState,
)
from .serialization import atomic_write_model, list_models, read_model, storage_key
from ._filesystem_common import _RepositoryBase, validated_copy


class FileSystemCycleInboxRepository(_RepositoryBase):
    async def create_if_absent(self, item: CycleInboxItem) -> CycleInboxItem:
        async with self.locks.hold(self.root, item.session_id):
            records = await self.list_for_cycle(item.cycle_id)
            for existing in records:
                if existing.inbox_item_id == item.inbox_item_id or existing.admission_id == item.admission_id or existing.input_batch_id == item.input_batch_id:
                    return existing
            if any(existing.cycle_sequence == item.cycle_sequence for existing in records):
                raise InputRuntimeConflictError("duplicate inbox cycle sequence")
            atomic_write_model(self.layout.inbox_item(item.cycle_id, item.inbox_item_id), item)
            return item

    async def list_for_cycle(self, cycle_id: str) -> tuple[CycleInboxItem, ...]:
        records = list_models(self.layout.inbox(cycle_id), CycleInboxItem)
        return tuple(sorted(records, key=lambda item: item.cycle_sequence))

    async def claim_contiguous_range(self, cycle_id: str, *, generation: int, after_sequence: int, max_items: int, max_bytes: int, lease_seconds: int) -> ClaimedInboxRange | None:
        records = await self.list_for_cycle(cycle_id)
        session_id = next((item.session_id for item in records), cycle_id)
        async with self.locks.hold(self.root, session_id):
            records = await self.list_for_cycle(cycle_id)
            candidates = [item for item in records if item.generation == generation and item.cycle_sequence > after_sequence and item.state == InboxState.QUEUED]
            if not candidates:
                return None
            candidates.sort(key=lambda item: item.cycle_sequence)
            if candidates[0].cycle_sequence != after_sequence + 1:
                return None
            selected: list[CycleInboxItem] = []
            total_bytes = 0
            expected = after_sequence + 1
            for item in candidates:
                if item.cycle_sequence != expected or len(selected) >= max_items:
                    break
                item_bytes = len(item.model_dump_json().encode("utf-8"))
                if selected and total_bytes + item_bytes > max_bytes:
                    break
                if not selected and item_bytes > max_bytes:
                    return None
                selected.append(item)
                total_bytes += item_bytes
                expected += 1
            now = datetime.now(timezone.utc)
            expires = now + timedelta(seconds=lease_seconds)
            token = uuid4().hex
            claimed = []
            for item in selected:
                updated = validated_copy(item, state=InboxState.CLAIMED, claim_token=token, claimed_at=now, claim_expires_at=expires, attempt_count=item.attempt_count + 1)
                atomic_write_model(self.layout.inbox_item(cycle_id, item.inbox_item_id), updated)
                claimed.append(updated)
            return ClaimedInboxRange(cycle_id=cycle_id, generation=generation, claim_token=token, first_cycle_sequence=claimed[0].cycle_sequence, last_cycle_sequence=claimed[-1].cycle_sequence, items=tuple(claimed), claimed_bytes=total_bytes, claim_expires_at=expires)

    async def _transition_claim(self, claim: ClaimedInboxRange, *, required_states: set[InboxState], next_state: InboxState, applied_at: datetime | None = None, error_code: str | None = None) -> tuple[CycleInboxItem, ...]:
        session_id = claim.items[0].session_id
        async with self.locks.hold(self.root, session_id):
            current_items = []
            for claimed in claim.items:
                path = self.layout.inbox_item(claim.cycle_id, claimed.inbox_item_id)
                current = read_model(path, CycleInboxItem)
                if current.state not in required_states or current.claim_token != claim.claim_token or current.generation != claim.generation:
                    raise InputRuntimeConflictError("stale or mismatched inbox claim")
                current_items.append((path, current))
            updated_items = []
            for path, current in current_items:
                updates: dict[str, object] = {"state": next_state}
                if next_state == InboxState.APPLIED:
                    updates.update(applied_at=applied_at, claim_token=None, claimed_at=None, claim_expires_at=None, last_error_code=None)
                elif next_state == InboxState.QUEUED:
                    updates.update(claim_token=None, claimed_at=None, claim_expires_at=None, last_error_code=error_code)
                updated = validated_copy(current, **updates)
                atomic_write_model(path, updated)
                updated_items.append(updated)
            return tuple(updated_items)

    async def mark_applying(self, claim: ClaimedInboxRange) -> ClaimedInboxRange:
        items = await self._transition_claim(claim, required_states={InboxState.CLAIMED}, next_state=InboxState.APPLYING)
        return validated_copy(claim, items=items)

    async def mark_applied(self, claim: ClaimedInboxRange, *, applied_at: datetime) -> tuple[CycleInboxItem, ...]:
        return await self._transition_claim(claim, required_states={InboxState.CLAIMED, InboxState.APPLYING}, next_state=InboxState.APPLIED, applied_at=applied_at)

    async def requeue_claim(self, claim: ClaimedInboxRange, *, error_code: str | None = None) -> tuple[CycleInboxItem, ...]:
        return await self._transition_claim(claim, required_states={InboxState.CLAIMED, InboxState.APPLYING}, next_state=InboxState.QUEUED, error_code=error_code)

    async def recover_expired_claims(self, *, now: datetime) -> tuple[CycleInboxItem, ...]:
        changed = []
        cycles = self.layout.root / "cycles"
        if not cycles.exists():
            return ()
        for path in sorted(cycles.glob("*/inbox/*.json")):
            item = read_model(path, CycleInboxItem)
            if item.state in {InboxState.CLAIMED, InboxState.APPLYING} and item.claim_expires_at is not None and item.claim_expires_at <= now:
                async with self.locks.hold(self.root, item.session_id):
                    current = read_model(path, CycleInboxItem)
                    if current.state not in {InboxState.CLAIMED, InboxState.APPLYING} or current.claim_expires_at is None or current.claim_expires_at > now:
                        continue
                    applied = False
                    if current.state == InboxState.APPLYING:
                        snapshot_path = self.layout.snapshot(current.cycle_id)
                        if snapshot_path.exists():
                            snapshot = read_model(snapshot_path, ActiveCycleSnapshot)
                            applied = snapshot.generation == current.generation and snapshot.applied_through_cycle_sequence >= current.cycle_sequence and current.input_batch_id in snapshot.applied_input_batch_ids
                    if applied:
                        updated = validated_copy(current, state=InboxState.APPLIED, applied_at=now, claim_token=None, claimed_at=None, claim_expires_at=None, last_error_code=None)
                    else:
                        updated = validated_copy(current, state=InboxState.QUEUED, claim_token=None, claimed_at=None, claim_expires_at=None, last_error_code="claim_expired")
                    atomic_write_model(path, updated)
                    changed.append(updated)
        return tuple(changed)

    async def cancel_generation(self, session_id: str, *, generation: int, cancelled_at: datetime, reason_code: str) -> tuple[CycleInboxItem, ...]:
        changed = []
        cycles = self.layout.root / "cycles"
        if not cycles.exists():
            return ()
        async with self.locks.hold(self.root, session_id):
            for path in sorted(cycles.glob("*/inbox/*.json")):
                item = read_model(path, CycleInboxItem)
                if item.session_id == session_id and item.generation == generation and item.state in {InboxState.QUEUED, InboxState.CLAIMED, InboxState.APPLYING}:
                    updated = validated_copy(item, state=InboxState.CANCELLED, cancelled_at=cancelled_at, claim_token=None, claimed_at=None, claim_expires_at=None, last_error_code=reason_code)
                    atomic_write_model(path, updated)
                    changed.append(updated)
        return tuple(changed)


class FileSystemActiveCycleSnapshotRepository(_RepositoryBase):
    async def create_if_absent(self, snapshot: ActiveCycleSnapshot) -> ActiveCycleSnapshot:
        async with self.locks.hold(self.root, snapshot.session_id):
            path = self.layout.snapshot(snapshot.cycle_id)
            if path.exists():
                return read_model(path, ActiveCycleSnapshot)
            atomic_write_model(path, snapshot)
            return snapshot

    async def get(self, cycle_id: str) -> ActiveCycleSnapshot | None:
        path = self.layout.snapshot(cycle_id)
        return read_model(path, ActiveCycleSnapshot) if path.exists() else None

    async def compare_and_swap(self, expected_revision: int, snapshot: ActiveCycleSnapshot) -> ActiveCycleSnapshot:
        async with self.locks.hold(self.root, snapshot.session_id):
            path = self.layout.snapshot(snapshot.cycle_id)
            if not path.exists():
                raise InputRuntimeNotFoundError(snapshot.cycle_id)
            current = read_model(path, ActiveCycleSnapshot)
            if current.snapshot_revision != expected_revision:
                raise InputRuntimeConflictError("stale snapshot revision")
            if snapshot.snapshot_revision != expected_revision + 1:
                raise InputRuntimeConflictError("snapshot revision must advance by one")
            atomic_write_model(path, snapshot)
            return snapshot

    async def _all(self) -> tuple[ActiveCycleSnapshot, ...]:
        cycles = self.layout.root / "cycles"
        records = []
        if cycles.exists():
            for path in sorted(cycles.glob("*/snapshot.json")):
                records.append(read_model(path, ActiveCycleSnapshot))
        return tuple(records)

    async def list_active(self) -> tuple[ActiveCycleSnapshot, ...]:
        terminal = {CycleStatus.DONE, CycleStatus.ERROR, CycleStatus.CANCELLED}
        return tuple(item for item in await self._all() if item.status not in terminal)

    async def list_resumable(self) -> tuple[ActiveCycleSnapshot, ...]:
        resumable = {CycleStatus.RUNNING, CycleStatus.WAITING_USER, CycleStatus.PAUSE_REQUESTED, CycleStatus.PAUSED_BY_USER, CycleStatus.INTERRUPTED, CycleStatus.FINALIZING}
        return tuple(item for item in await self._all() if item.status in resumable)

    async def cancel_generation(self, session_id: str, *, generation: int, reason_code: str) -> ActiveCycleSnapshot | None:
        for snapshot in await self._all():
            if snapshot.session_id == session_id and snapshot.generation == generation:
                updated = validated_copy(snapshot, status=CycleStatus.CANCELLED, interruption_reason=reason_code, waiting_question=None, pause_reason=None, snapshot_revision=snapshot.snapshot_revision + 1, updated_at=datetime.now(timezone.utc))
                async with self.locks.hold(self.root, session_id):
                    atomic_write_model(self.layout.snapshot(snapshot.cycle_id), updated)
                return updated
        return None


class FileSystemContextRevisionRepository(_RepositoryBase):
    async def append_revision(self, revision: CycleContextRevision) -> CycleContextRevision:
        async with self.locks.hold(self.root, revision.session_id):
            path = self.layout.revision(revision.cycle_id, revision.context_revision_id)
            if path.exists():
                existing = read_model(path, CycleContextRevision)
                if existing == revision:
                    return existing
                raise InputRuntimeConflictError("context revision ID conflict")
            latest = await self.get_latest(revision.cycle_id)
            if latest is None and revision.revision_number != 1:
                raise InputRuntimeConflictError("first context revision must be 1")
            if latest is not None:
                if revision.revision_number != latest.revision_number + 1:
                    raise InputRuntimeConflictError("context revision sequence gap")
                if revision.parent_revision_ids != [latest.context_revision_id]:
                    raise InputRuntimeConflictError("context revision parent mismatch")
            atomic_write_model(path, revision)
            return revision

    async def get(self, context_revision_id: str) -> CycleContextRevision | None:
        cycles = self.layout.root / "cycles"
        if cycles.exists():
            name = f"{storage_key(context_revision_id)}.json"
            for path in sorted(cycles.glob(f"*/context-revisions/{name}")):
                return read_model(path, CycleContextRevision)
        return None

    async def get_latest(self, cycle_id: str) -> CycleContextRevision | None:
        records = await self.list_for_cycle(cycle_id)
        return records[-1] if records else None

    async def list_for_cycle(self, cycle_id: str) -> tuple[CycleContextRevision, ...]:
        records = list_models(self.layout.revisions(cycle_id), CycleContextRevision)
        return tuple(sorted(records, key=lambda item: item.revision_number))
