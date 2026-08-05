import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.input_runtime import (
    ActiveCycleSnapshot,
    CheckpointName,
    CycleInboxItem,
    CycleStatus,
    InboxState,
    create_filesystem_input_runtime_repositories,
    new_admission_id,
    new_context_revision_id,
)
from src.input_runtime.errors import InputRuntimeConflictError
from src.storage import StorageConfigType

NOW = datetime(2026, 8, 5, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def repos(tmp_path: Path):
    return create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )


def item(sequence: int, *, batch=None):
    return CycleInboxItem(
        admission_id=new_admission_id(),
        session_id="session",
        cycle_id="cycle",
        input_batch_id=batch or f"batch-{sequence}",
        cycle_sequence=sequence,
        generation=0,
        enqueued_at=NOW,
    )


def snapshot(*, applied, batches):
    return ActiveCycleSnapshot(
        cycle_id="cycle",
        session_id="session",
        generation=0,
        status=CycleStatus.RUNNING,
        original_input_batch_id="initial",
        original_user_request="request",
        applied_input_batch_ids=batches,
        applied_through_cycle_sequence=applied,
        active_context_revision_id=new_context_revision_id(),
        snapshot_revision=1,
        safe_checkpoint=CheckpointName.AFTER_TOOL_BLOCK,
        created_at=NOW,
        updated_at=NOW,
    )


def test_claim_is_contiguous_bounded_and_token_fenced(tmp_path):
    repository = repos(tmp_path).inbox
    for sequence in (1, 2, 3):
        run(repository.create_if_absent(item(sequence)))
    claim = run(repository.claim_contiguous_range(
        "cycle", generation=0, after_sequence=0,
        max_items=2, max_bytes=1_000_000, lease_seconds=300,
    ))
    assert claim is not None
    assert [record.cycle_sequence for record in claim.items] == [1, 2]
    applying = run(repository.mark_applying(claim))
    assert all(record.state == InboxState.APPLYING for record in applying.items)
    stale = claim.model_copy(update={"claim_token": "stale"})
    with pytest.raises((InputRuntimeConflictError, ValueError)):
        run(repository.mark_applied(stale, applied_at=NOW))
    applied = run(repository.mark_applied(applying, applied_at=NOW))
    assert all(record.state == InboxState.APPLIED for record in applied)
    next_claim = run(repository.claim_contiguous_range(
        "cycle", generation=0, after_sequence=2,
        max_items=8, max_bytes=1_000_000, lease_seconds=300,
    ))
    assert next_claim.first_cycle_sequence == 3


def test_expired_claim_requeues_after_restart(tmp_path):
    repository = repos(tmp_path).inbox
    run(repository.create_if_absent(item(1)))
    claim = run(repository.claim_contiguous_range(
        "cycle", generation=0, after_sequence=0,
        max_items=8, max_bytes=1_000_000, lease_seconds=1,
    ))
    recovered = run(repos(tmp_path).inbox.recover_expired_claims(
        now=claim.claim_expires_at + timedelta(seconds=1)
    ))
    assert recovered[0].state == InboxState.QUEUED
    assert recovered[0].claim_token is None


def test_expired_applying_claim_reconciles_snapshot_watermark(tmp_path):
    repositories = repos(tmp_path)
    queued = item(1)
    run(repositories.inbox.create_if_absent(queued))
    claim = run(repositories.inbox.claim_contiguous_range(
        "cycle", generation=0, after_sequence=0,
        max_items=8, max_bytes=1_000_000, lease_seconds=1,
    ))
    run(repositories.inbox.mark_applying(claim))
    run(repositories.snapshots.create_if_absent(
        snapshot(applied=1, batches=[queued.input_batch_id])
    ))
    recovered = run(repos(tmp_path).inbox.recover_expired_claims(
        now=claim.claim_expires_at + timedelta(seconds=1)
    ))
    assert recovered[0].state == InboxState.APPLIED
    assert recovered[0].applied_at is not None


def test_duplicate_inbox_admission_returns_existing(tmp_path):
    repository = repos(tmp_path).inbox
    first = item(1)
    assert run(repository.create_if_absent(first)) == first
    duplicate = item(2).model_copy(update={
        "admission_id": first.admission_id,
        "input_batch_id": first.input_batch_id,
    })
    assert run(repository.create_if_absent(duplicate)) == first
