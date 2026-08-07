from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.input_runtime import (
    CheckpointAction,
    CheckpointName,
    CycleStatus,
    InputAdmissionService,
    InputRuntimeConfigType,
    create_filesystem_input_runtime_repositories,
)
from src.input_runtime import ir5_filesystem_controls as filesystem_controls
from src.runtime import ActiveAgentCycle
from src.storage import StorageConfigType


NOW = datetime(2026, 8, 7, 15, 0, tzinfo=timezone.utc)


@dataclass
class TextPart:
    part_id: str
    kind: str
    text: str
    attachment_slot_ids: list[str] = field(default_factory=list)


@dataclass
class Batch:
    input_batch_id: str
    session_id: str = "session"
    payload_size: int = 10
    text_parts: list[TextPart] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    source_event_ids: tuple[str, ...] = ("evt_" + "8" * 32,)
    content_fingerprint: str = "sha256:" + "9" * 64
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
        self.batches = {item.input_batch_id: item for item in batches}

    async def get_committed(self, input_batch_id: str):
        return self.batches[input_batch_id]


class Wake:
    async def wake(self, session_id: str, *, cycle_id: str) -> bool:
        return True


class ContinueBeforeBoundaryBarrier:
    """Hold request_continue immediately before repository durable acceptance."""

    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.events: list[str] = []
        self.used = False

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    async def accept_continue(self, command):
        if not self.used:
            self.used = True
            self.entered.set()
            await self.release.wait()
        result = await self.delegate.accept_continue(command)
        self.events.append("continue_durable")
        return result


class AdmissionBeforeBoundaryBarrier:
    """Hold one input immediately before its repository durable allocation."""

    def __init__(self, delegate, *, input_batch_id: str) -> None:
        self.delegate = delegate
        self.input_batch_id = input_batch_id
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.events: list[str] = []
        self.used = False

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    async def allocate(self, record):
        if record.input_batch_id == self.input_batch_id and not self.used:
            self.used = True
            self.entered.set()
            await self.release.wait()
        result = await self.delegate.allocate(record)
        if record.input_batch_id == self.input_batch_id:
            self.events.append("input_durable")
        return result


def make_service(tmp_path, *, repositories=None):
    repositories = repositories or create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )
    batches = (
        Batch("initial"),
        Batch(
            "input-a",
            text_parts=[TextPart("part-a", "message_text", "real answer A")],
        ),
    )
    service = InputAdmissionService(
        config=InputRuntimeConfigType(max_batches_per_checkpoint=1),
        repositories=repositories,
        committed_batches=Reader(*batches),
        wake_coordinator=Wake(),
        cycle_id_factory=lambda: "cycle-a",
        clock=lambda: NOW,
        payload_size_resolver=lambda batch: batch.payload_size,
    )
    return service, repositories


def active_cycle() -> ActiveAgentCycle:
    return ActiveAgentCycle(
        cycle_id="cycle-a",
        session_id="session",
        original_user_request="initial",
        messages_for_llm=[
            {"role": "system", "content": "system"},
            {
                "role": "user",
                "content": json.dumps(
                    {"type": "user_request", "user_request": "initial"}
                ),
            },
        ],
        cycle_trace=[],
        original_user_message_index=1,
        original_input_batch_id="initial",
        input_runtime_generation=0,
    )


async def start_paused(service: InputAdmissionService) -> ActiveAgentCycle:
    admitted = await service.admit_committed_batch(
        "initial",
        session_id="session",
    )
    assert admitted.target_cycle_id == "cycle-a"
    active = active_cycle()
    initial = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.RESUME,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert initial.action in {
        CheckpointAction.CONTINUE,
        CheckpointAction.INPUT_APPLIED,
    }
    pause = await service.control_service.request_pause(
        session_id="session",
        idempotency_key="pause-first",
        source_client_type="test",
        source_message_ref={"message_id": 1},
    )
    assert pause.command.target_cycle_id == "cycle-a"
    paused = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.BEFORE_LLM,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert paused.action == CheckpointAction.PAUSE
    return active


def resume_target(service: InputAdmissionService, command) -> int | None:
    return service.control_service.resume_input_target(command)


def input_updates(active: ActiveAgentCycle) -> list[dict]:
    updates = []
    for message in active.messages_for_llm:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            payload = json.loads(content)
        except Exception:
            continue
        if isinstance(payload, dict) and payload.get("type") == "input_batch_update":
            updates.append(payload)
    return updates


@pytest.mark.asyncio
async def test_input_durable_before_continue_boundary_is_frozen_and_drained(tmp_path):
    base = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )
    barrier = ContinueBeforeBoundaryBarrier(base.controls)
    service, _ = make_service(
        tmp_path,
        repositories=replace(base, controls=barrier),
    )
    active = await start_paused(service)
    before_revisions = await base.context_revisions.list_for_cycle("cycle-a")

    continue_task = asyncio.create_task(
        service.control_service.request_continue(
            session_id="session",
            idempotency_key="continue-race-a",
            source_client_type="test",
            source_message_ref={"message_id": 2},
        )
    )
    await barrier.entered.wait()

    admission = await service.admit_committed_batch(
        "input-a",
        session_id="session",
    )
    barrier.events.append("input_durable")
    state_after_input = await base.sessions.get("session")
    assert admission.target_cycle_id == "cycle-a"
    assert state_after_input.active_cycle_accepted_through_sequence == 1

    barrier.release.set()
    continued = await continue_task
    assert barrier.events == ["input_durable", "continue_durable"]
    assert resume_target(service, continued.command) == 1

    resumed = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.RESUME,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert resumed.action == CheckpointAction.INPUT_APPLIED
    assert resumed.applied_input_batch_ids == ("input-a",)
    assert resumed.applied_through_cycle_sequence == 1

    snapshot_at_first_meaningful_llm = await base.snapshots.get("cycle-a")
    state_at_first_meaningful_llm = await base.sessions.get("session")
    assert snapshot_at_first_meaningful_llm.applied_through_cycle_sequence == 1
    assert state_at_first_meaningful_llm.active_cycle_applied_through_sequence == 1
    assert state_at_first_meaningful_llm.active_cycle_id == "cycle-a"
    assert state_at_first_meaningful_llm.cycle_status == CycleStatus.RUNNING

    updates = input_updates(active)
    assert len(updates) == 1
    assert updates[0]["batches"][0]["input_batch_id"] == "input-a"
    assert all(
        "/continue" not in str(message.get("content") or "")
        for message in active.messages_for_llm
    )
    after_revisions = await base.context_revisions.list_for_cycle("cycle-a")
    assert len(after_revisions) == len(before_revisions) + 1


@pytest.mark.asyncio
async def test_input_durable_after_continue_boundary_is_excluded_until_running_checkpoint(tmp_path):
    base = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )
    barrier = AdmissionBeforeBoundaryBarrier(
        base.admissions,
        input_batch_id="input-a",
    )
    service, _ = make_service(
        tmp_path,
        repositories=replace(base, admissions=barrier),
    )
    active = await start_paused(service)
    before_revisions = await base.context_revisions.list_for_cycle("cycle-a")

    input_task = asyncio.create_task(
        service.admit_committed_batch("input-a", session_id="session")
    )
    await barrier.entered.wait()

    continued = await service.control_service.request_continue(
        session_id="session",
        idempotency_key="continue-race-b",
        source_client_type="test",
        source_message_ref={"message_id": 3},
    )
    ordering = ["continue_durable"]
    assert resume_target(service, continued.command) == 0

    barrier.release.set()
    await input_task
    ordering.extend(barrier.events)
    assert ordering == ["continue_durable", "input_durable"]
    state_after_input = await base.sessions.get("session")
    assert state_after_input.active_cycle_accepted_through_sequence == 1

    resumed = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.RESUME,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert resumed.applied_input_batch_ids == ()
    snapshot = await base.snapshots.get("cycle-a")
    assert snapshot.applied_through_cycle_sequence == 0
    rows = await base.inbox.list_for_cycle("cycle-a")
    assert [(row.input_batch_id, row.state.value) for row in rows] == [
        ("input-a", "queued")
    ]
    after_resume_revisions = await base.context_revisions.list_for_cycle("cycle-a")
    assert len(after_resume_revisions) == len(before_revisions)

    ordinary = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.BEFORE_LLM,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert ordinary.applied_input_batch_ids == ("input-a",)
    assert ordinary.applied_through_cycle_sequence == 1


@pytest.mark.asyncio
async def test_duplicate_continue_preserves_original_frozen_target_after_late_input(tmp_path):
    service, repositories = make_service(tmp_path)
    await start_paused(service)

    first = await service.control_service.request_continue(
        session_id="session",
        idempotency_key="continue-duplicate",
        source_client_type="test",
        source_message_ref={"message_id": 4},
    )
    assert resume_target(service, first.command) == 0

    await service.admit_committed_batch("input-a", session_id="session")
    duplicate = await service.control_service.request_continue(
        session_id="session",
        idempotency_key="continue-duplicate",
        source_client_type="test",
        source_message_ref={"message_id": 4},
    )

    assert duplicate.command.control_id == first.command.control_id
    assert duplicate.command.sequence_number == first.command.sequence_number
    assert duplicate.command.source_message_ref == first.command.source_message_ref
    assert resume_target(service, duplicate.command) == 0
    state = await repositories.sessions.get("session")
    assert state.active_cycle_accepted_through_sequence == 1


@pytest.mark.asyncio
async def test_continue_record_first_crash_preserves_frozen_target_and_repairs_frontier(
    tmp_path,
    monkeypatch,
):
    service, repositories = make_service(tmp_path)
    await start_paused(service)
    real_write = filesystem_controls.atomic_write_model
    failed = False

    def fail_pending_watermark_once(path, model):
        nonlocal failed
        if (
            not failed
            and path.name == "state.json"
            and getattr(model, "pending_control_sequence", 0) == 2
        ):
            failed = True
            raise OSError("continue pending watermark fault")
        return real_write(path, model)

    monkeypatch.setattr(
        filesystem_controls,
        "atomic_write_model",
        fail_pending_watermark_once,
    )
    with pytest.raises(OSError, match="continue pending watermark fault"):
        await service.control_service.request_continue(
            session_id="session",
            idempotency_key="continue-crash",
            source_client_type="test",
            source_message_ref={"message_id": 5},
        )

    persisted = await repositories.controls.get_by_idempotency_key(
        "session",
        "continue-crash",
    )
    assert persisted is not None
    assert persisted.sequence_number == 2
    assert resume_target(service, persisted) == 0
    stale_state = await repositories.sessions.get("session")
    assert stale_state.pending_control_sequence == 1

    recreated, recreated_repositories = make_service(tmp_path)
    retried = await recreated.control_service.request_continue(
        session_id="session",
        idempotency_key="continue-crash",
        source_client_type="test",
        source_message_ref={"message_id": 5},
    )
    assert retried.command.control_id == persisted.control_id
    assert retried.command.sequence_number == persisted.sequence_number
    assert retried.command.source_message_ref == persisted.source_message_ref
    assert resume_target(recreated, retried.command) == 0
    repaired_state = await recreated_repositories.sessions.get("session")
    assert repaired_state.pending_control_sequence == 2

    next_control = await recreated.control_service.request_pause(
        session_id="session",
        idempotency_key="pause-after-continue-crash",
        source_client_type="test",
        source_message_ref={"message_id": 6},
    )
    assert next_control.command.sequence_number == 3
    sequences = [
        row.sequence_number
        for row in await recreated_repositories.controls.list_for_session("session")
    ]
    assert len(sequences) == len(set(sequences))
