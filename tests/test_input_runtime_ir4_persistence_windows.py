from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.input_runtime import (
    CheckpointName,
    CycleStatus,
    InputAdmissionService,
    InputRuntimeConfigType,
    RuntimeHandoffState,
    create_filesystem_input_runtime_repositories,
)
from src.runtime import ActiveAgentCycle
from src.storage import StorageConfigType


NOW = datetime(2026, 8, 6, 20, 0, tzinfo=timezone.utc)


@dataclass
class Batch:
    input_batch_id: str
    session_id: str = "session"
    payload_size: int = 10
    text_parts: list[object] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    source_event_ids: tuple[str, ...] = ("evt_" + "1" * 32,)
    content_fingerprint: str = "sha256:" + "2" * 64
    committed_at: datetime = NOW
    continuation_of_batch_id: str | None = None
    correction_of_batch_id: str | None = None
    artifact_manifest: object = field(
        default_factory=lambda: SimpleNamespace(items=())
    )

    def model_dump_json(self) -> str:
        return "x" * self.payload_size


class Reader:
    def __init__(self, *batches: Batch) -> None:
        self.batches = {batch.input_batch_id: batch for batch in batches}

    async def get_committed(self, input_batch_id: str):
        return self.batches[input_batch_id]


class Wake:
    async def wake(self, session_id: str, *, cycle_id: str) -> bool:
        return True


def active_cycle() -> ActiveAgentCycle:
    return ActiveAgentCycle(
        cycle_id="cycle-a",
        session_id="session",
        original_user_request="initial",
        messages_for_llm=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": '{"type":"user_request"}'},
        ],
        cycle_trace=[],
        original_user_message_index=1,
        original_input_batch_id="initial",
        input_runtime_generation=0,
    )


def build_runtime(tmp_path, batches, *, repositories=None):
    repositories = repositories or create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )
    service = InputAdmissionService(
        config=InputRuntimeConfigType(
            max_batches_per_checkpoint=8,
            max_batch_bytes_per_checkpoint=1000,
            max_queued_batches_per_session=32,
            max_queued_bytes_per_session=100000,
        ),
        repositories=repositories,
        committed_batches=Reader(*batches),
        wake_coordinator=Wake(),
        cycle_id_factory=lambda: "cycle-a",
        clock=lambda: NOW,
        payload_size_resolver=lambda batch: batch.payload_size,
    )
    return service, repositories


async def initialize(service: InputAdmissionService) -> ActiveAgentCycle:
    await service.admit_committed_batch("initial", session_id="session")
    active = active_cycle()
    await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.RESUME,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    return active


async def assert_single_apply_after_retry(service, base, active):
    await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.BEFORE_LLM,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    revisions = await base.context_revisions.list_for_cycle("cycle-a")
    assert len(revisions) == 2
    updates = []
    for message in active.messages_for_llm:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        payload = json.loads(content)
        if payload.get("type") == "input_batch_update":
            updates.append(payload)
    assert len(updates) == 1
    assert updates[0]["batches"][0]["input_batch_id"] == "addition"


class DurableClaimReturnBarrier:
    """Persist CLAIMED records, then block before returning the exact claim."""

    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.claim_written = asyncio.Event()
        self.claim_release = asyncio.Event()
        self.cleanup_entered = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.used = False

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    async def claim_contiguous_range(self, *args, **kwargs):
        claim = await self.delegate.claim_contiguous_range(*args, **kwargs)
        if claim is not None and not self.used:
            self.used = True
            self.claim_written.set()
            await self.claim_release.wait()
        return claim

    async def requeue_claim(self, claim, *, error_code=None):
        self.cleanup_entered.set()
        await self.cleanup_release.wait()
        return await self.delegate.requeue_claim(claim, error_code=error_code)


@pytest.mark.asyncio
async def test_claim_return_window_is_cancelled_and_cleaned_without_duplicate(tmp_path):
    base = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )
    barrier = DurableClaimReturnBarrier(base.inbox)
    service, _ = build_runtime(
        tmp_path,
        [Batch("initial"), Batch("addition")],
        repositories=replace(base, inbox=barrier),
    )
    active = await initialize(service)
    await service.admit_committed_batch("addition", session_id="session")

    task = asyncio.create_task(
        service.checkpoint_service.run_checkpoint(
            checkpoint=CheckpointName.BEFORE_LLM,
            active_cycle=active,
            desired_status=CycleStatus.RUNNING,
        )
    )
    await barrier.claim_written.wait()
    rows = await base.inbox.list_for_cycle("cycle-a")
    assert rows[0].state.value == "claimed"

    task.cancel()
    task.cancel()
    barrier.claim_release.set()
    await barrier.cleanup_entered.wait()
    task.cancel()
    task.cancel()
    barrier.cleanup_release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    rows = await base.inbox.list_for_cycle("cycle-a")
    assert rows[0].state.value == "queued"
    assert rows[0].claim_token is None
    assert len(await base.context_revisions.list_for_cycle("cycle-a")) == 1

    await assert_single_apply_after_retry(service, base, active)


class FailHandoffCompletion:
    def __init__(self, delegate) -> None:
        self.delegate = delegate

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    async def complete(self, *args, **kwargs):
        raise OSError("handoff completion failed")


async def prepare_terminal(service, repositories, *, token: str):
    active = await initialize(service)
    admission = await repositories.admissions.get_by_input_batch_id("initial")
    assert admission is not None
    assert await service.begin_runtime_handoff(admission, handoff_token=token)
    await service.record_cycle_status(
        session_id="session",
        cycle_id="cycle-a",
        status=CycleStatus.DONE,
    )
    return active, admission


@pytest.mark.asyncio
async def test_handoff_completion_failure_precedes_terminal_snapshot(tmp_path):
    base = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )
    service, repositories = build_runtime(
        tmp_path,
        [Batch("initial")],
        repositories=replace(
            base,
            handoffs=FailHandoffCompletion(base.handoffs),
        ),
    )
    _active, admission = await prepare_terminal(
        service,
        repositories,
        token="fail-complete-token",
    )

    with pytest.raises(OSError, match="handoff completion failed"):
        await service.complete_runtime_handoff(
            admission,
            handoff_token="fail-complete-token",
        )

    snapshot = await base.snapshots.get("cycle-a")
    marker = await base.handoffs.get(admission.admission_id)
    assert snapshot.status == CycleStatus.RUNNING
    assert marker.state == RuntimeHandoffState.HANDED_OFF

    marker = await service.mark_runtime_handoff_ambiguous(
        admission,
        handoff_token="fail-complete-token",
        error_code="terminal_handoff_completion_failed",
    )
    await service.record_cycle_status(
        session_id="session",
        cycle_id="cycle-a",
        status=CycleStatus.INTERRUPTED,
    )
    state = await base.sessions.get("session")
    snapshot = await base.snapshots.get("cycle-a")
    assert marker.state == RuntimeHandoffState.AMBIGUOUS
    assert state.cycle_status == CycleStatus.INTERRUPTED
    assert snapshot.status == CycleStatus.RUNNING


@pytest.mark.asyncio
async def test_completed_handoff_survives_terminal_snapshot_failure_and_fences_duplicate(
    tmp_path,
):
    service, repositories = build_runtime(tmp_path, [Batch("initial")])
    _active, admission = await prepare_terminal(
        service,
        repositories,
        token="snapshot-fail-token",
    )

    async def fail_sync(**_kwargs):
        raise OSError("terminal snapshot sync failed")

    service.checkpoint_service.sync_terminal_snapshot = fail_sync
    with pytest.raises(OSError, match="terminal snapshot sync failed"):
        await service.complete_runtime_handoff(
            admission,
            handoff_token="snapshot-fail-token",
        )

    marker = await repositories.handoffs.get(admission.admission_id)
    snapshot = await repositories.snapshots.get("cycle-a")
    state = await repositories.sessions.get("session")
    assert marker.state == RuntimeHandoffState.COMPLETED
    assert state.cycle_status == CycleStatus.DONE
    assert snapshot.status == CycleStatus.RUNNING

    unchanged = await service.mark_runtime_handoff_ambiguous(
        admission,
        handoff_token="snapshot-fail-token",
        error_code="must_not_replace_completed",
    )
    assert unchanged.state == RuntimeHandoffState.COMPLETED

    duplicate = await service.admit_committed_batch(
        "initial",
        session_id="session",
    )
    assert duplicate.should_start_runner is False
    assert duplicate.should_wake_runner is False
    assert (
        await repositories.handoffs.get(admission.admission_id)
    ).state == RuntimeHandoffState.COMPLETED


@pytest.mark.asyncio
async def test_normal_terminal_order_completes_handoff_then_snapshot(tmp_path):
    service, repositories = build_runtime(tmp_path, [Batch("initial")])
    _active, admission = await prepare_terminal(
        service,
        repositories,
        token="normal-terminal-token",
    )

    marker = await service.complete_runtime_handoff(
        admission,
        handoff_token="normal-terminal-token",
    )
    snapshot = await repositories.snapshots.get("cycle-a")
    state = await repositories.sessions.get("session")
    assert marker.state == RuntimeHandoffState.COMPLETED
    assert marker.completed_at is not None
    assert state.cycle_status == CycleStatus.DONE
    assert snapshot.status == CycleStatus.DONE
