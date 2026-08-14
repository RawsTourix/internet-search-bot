from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.input_runtime import (
    CheckpointAction,
    CheckpointName,
    ControlCommandType,
    ControlState,
    CycleStatus,
    InputAdmissionAction,
    InputAdmissionService,
    InputRuntimeConfigType,
    SessionInputRuntimeState,
    create_filesystem_input_runtime_repositories,
)
from src.input_runtime import ir5_filesystem_controls as control_fs
from src.runtime import ActiveAgentCycle
from src.storage import StorageConfigType


NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)


@dataclass
class FakeTextPart:
    part_id: str
    kind: str
    text: str
    attachment_slot_ids: list[str] = field(default_factory=list)


@dataclass
class FakeBatch:
    input_batch_id: str
    session_id: str = "session"
    payload_size: int = 10
    text_parts: list[FakeTextPart] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    source_event_ids: tuple[str, ...] = ("evt_" + "8" * 32,)
    content_fingerprint: str = "sha256:" + "9" * 64
    committed_at: datetime = NOW
    continuation_of_batch_id: str | None = None
    correction_of_batch_id: str | None = None
    artifact_manifest: object = field(default_factory=lambda: SimpleNamespace(items=()))

    def model_dump_json(self) -> str:
        return "x" * self.payload_size


class Reader:
    def __init__(self, *batches: FakeBatch):
        self.batches = {batch.input_batch_id: batch for batch in batches}

    async def get_committed(self, input_batch_id: str):
        return self.batches[input_batch_id]


class Wake:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def wake(self, session_id: str, *, cycle_id: str) -> bool:
        self.calls.append((session_id, cycle_id))
        return True


def make_service(root, batches, *, max_batches=1):
    repositories = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(root))
    )
    wake = Wake()
    service = InputAdmissionService(
        config=InputRuntimeConfigType(max_batches_per_checkpoint=max_batches),
        repositories=repositories,
        committed_batches=Reader(*batches),
        wake_coordinator=wake,
        cycle_id_factory=lambda: "cycle-a",
        clock=lambda: NOW,
        payload_size_resolver=lambda batch: batch.payload_size,
    )
    return service, repositories, wake


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


async def start(service):
    admission = await service.admit_committed_batch("initial", session_id="session")
    assert admission.target_cycle_id == "cycle-a"
    active = active_cycle()
    outcome = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.RESUME,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert outcome.action in {CheckpointAction.CONTINUE, CheckpointAction.INPUT_APPLIED}
    return active


async def set_waiting(repositories, active, question="Which city?"):
    state = await repositories.sessions.get("session")
    await repositories.sessions.compare_and_swap(
        state.revision,
        state.model_copy(
            update={
                "cycle_status": CycleStatus.WAITING_USER,
                "revision": state.revision + 1,
                "updated_at": NOW,
            }
        ),
    )
    snapshot = await repositories.snapshots.get(active.cycle_id)
    await repositories.snapshots.compare_and_swap(
        snapshot.snapshot_revision,
        snapshot.model_copy(
            update={
                "status": CycleStatus.WAITING_USER,
                "waiting_question": question,
                "snapshot_revision": snapshot.snapshot_revision + 1,
                "updated_at": NOW,
            }
        ),
    )
    active.waiting_question = question


def fail_next_control_state_write(monkeypatch):
    real_write = control_fs.atomic_write_model
    failed = False

    def fault(path, model):
        nonlocal failed
        if isinstance(model, SessionInputRuntimeState) and not failed:
            failed = True
            raise OSError("control state write fault")
        return real_write(path, model)

    monkeypatch.setattr(control_fs, "atomic_write_model", fault)
    return real_write


@pytest.mark.asyncio
async def test_record_first_pause_crash_then_independent_pause_uses_next_sequence(
    tmp_path, monkeypatch
):
    batches = [FakeBatch("initial")]
    service, repositories, _ = make_service(tmp_path, batches)
    await start(service)
    real_write = fail_next_control_state_write(monkeypatch)

    with pytest.raises(OSError, match="control state write fault"):
        await service.control_service.request_pause(
            session_id="session",
            idempotency_key="pause-a",
            source_client_type="test",
            source_message_ref={"message_id": 1},
        )
    a = await repositories.controls.get_by_idempotency_key("session", "pause-a")
    assert a is not None and a.sequence_number == 1
    assert (await repositories.sessions.get("session")).pending_control_sequence == 0

    monkeypatch.setattr(control_fs, "atomic_write_model", real_write)
    service2, repositories2, _ = make_service(tmp_path, batches)
    b = await service2.control_service.request_pause(
        session_id="session",
        idempotency_key="pause-b",
        source_client_type="test",
        source_message_ref={"message_id": 2},
    )
    rows = await repositories2.controls.list_for_session("session")
    assert [(row.idempotency_key, row.sequence_number) for row in rows] == [
        ("pause-a", 1),
        ("pause-b", 2),
    ]
    assert b.command.sequence_number == 2
    assert (await repositories2.sessions.get("session")).pending_control_sequence == 2


@pytest.mark.asyncio
async def test_record_first_pause_crash_then_reset_gets_next_sequence_and_one_generation(
    tmp_path, monkeypatch
):
    batches = [FakeBatch("initial")]
    service, repositories, _ = make_service(tmp_path, batches)
    await start(service)
    real_write = fail_next_control_state_write(monkeypatch)
    with pytest.raises(OSError):
        await service.control_service.request_pause(
            session_id="session",
            idempotency_key="pause-before-reset",
            source_client_type="test",
            source_message_ref={"message_id": 10},
        )

    monkeypatch.setattr(control_fs, "atomic_write_model", real_write)
    service2, repositories2, _ = make_service(tmp_path, batches)
    reset = await service2.control_service.request_reset(
        session_id="session",
        idempotency_key="reset-b",
        source_client_type="test",
        source_message_ref={"message_id": 11},
    )
    assert reset.command.sequence_number == 2
    state = await repositories2.sessions.get("session")
    assert state.generation == 1
    assert state.pending_control_sequence == state.applied_control_sequence == 2
    duplicate = await service2.control_service.request_reset(
        session_id="session",
        idempotency_key="reset-b",
        source_client_type="test",
        source_message_ref={"message_id": 11},
    )
    assert duplicate.command.control_id == reset.command.control_id
    assert (await repositories2.sessions.get("session")).generation == 1


@pytest.mark.asyncio
async def test_record_first_reset_crash_then_independent_control_does_not_reuse_sequence(
    tmp_path, monkeypatch
):
    batches = [FakeBatch("initial")]
    service, repositories, _ = make_service(tmp_path, batches)
    await start(service)
    real_write = fail_next_control_state_write(monkeypatch)
    with pytest.raises(OSError, match="control state write fault"):
        await service.control_service.request_reset(
            session_id="session",
            idempotency_key="reset-a",
            source_client_type="test",
            source_message_ref={"message_id": 20},
        )
    reset_a = await repositories.controls.get_by_idempotency_key("session", "reset-a")
    assert reset_a is not None and reset_a.sequence_number == 1
    assert (await repositories.sessions.get("session")).generation == 0

    monkeypatch.setattr(control_fs, "atomic_write_model", real_write)
    service2, repositories2, _ = make_service(tmp_path, batches)
    pause_b = await service2.control_service.request_pause(
        session_id="session",
        idempotency_key="pause-after-reset-record",
        source_client_type="test",
        source_message_ref={"message_id": 21},
    )
    assert pause_b.command.sequence_number == 2
    rows = await repositories2.controls.list_for_session("session")
    assert [row.sequence_number for row in rows] == [1, 2]
    assert len({row.sequence_number for row in rows}) == 2


@pytest.mark.asyncio
async def test_waiting_pause_with_real_paused_input_drains_target_then_runs(tmp_path):
    batches = [
        FakeBatch("initial"),
        FakeBatch("a", text_parts=[FakeTextPart("a", "message_text", "A")]),
        FakeBatch("b", text_parts=[FakeTextPart("b", "message_text", "B")]),
    ]
    service, repositories, _ = make_service(tmp_path, batches, max_batches=1)
    active = await start(service)
    await set_waiting(repositories, active)

    paused = await service.control_service.request_pause(
        session_id="session",
        idempotency_key="pause-waiting-input",
        source_client_type="test",
        source_message_ref={"message_id": 30},
    )
    assert paused.command.state == ControlState.APPLIED
    assert (await repositories.snapshots.get(active.cycle_id)).waiting_question == "Which city?"

    a = await service.admit_committed_batch("a", session_id="session")
    b = await service.admit_committed_batch("b", session_id="session")
    assert a.action == b.action == InputAdmissionAction.QUEUED_PAUSED

    cont = await service.control_service.request_continue(
        session_id="session",
        idempotency_key="continue-with-answer",
        source_client_type="test",
        source_message_ref={"message_id": 31},
    )
    assert service.control_service.resume_input_target(cont.command) == 2
    before_revision = (await repositories.snapshots.get(active.cycle_id)).active_context_revision_id

    outcome = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.RESUME,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert outcome.action == CheckpointAction.INPUT_APPLIED
    assert outcome.applied_input_batch_ids == ("a", "b")
    snapshot = await repositories.snapshots.get(active.cycle_id)
    state = await repositories.sessions.get("session")
    assert snapshot.status == CycleStatus.RUNNING
    assert state.cycle_status == CycleStatus.RUNNING
    assert snapshot.waiting_question is None
    assert snapshot.applied_through_cycle_sequence == 2
    assert snapshot.active_context_revision_id != before_revision
    assert active.cycle_id == "cycle-a"
    rendered = json.dumps(snapshot.messages_for_llm, ensure_ascii=False)
    assert "/continue" not in rendered


@pytest.mark.asyncio
async def test_waiting_pause_continue_without_input_still_waits(tmp_path):
    service, repositories, _ = make_service(tmp_path, [FakeBatch("initial")])
    active = await start(service)
    await set_waiting(repositories, active)
    await service.control_service.request_pause(
        session_id="session",
        idempotency_key="pause-no-input",
        source_client_type="test",
        source_message_ref={"message_id": 40},
    )
    await service.control_service.request_continue(
        session_id="session",
        idempotency_key="continue-no-input",
        source_client_type="test",
        source_message_ref={"message_id": 41},
    )
    outcome = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.RESUME,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert outcome.action == CheckpointAction.WAIT
    assert outcome.reason_code == "still_waiting_for_input"
    snapshot = await repositories.snapshots.get(active.cycle_id)
    assert snapshot.status == CycleStatus.WAITING_USER
    assert snapshot.waiting_question == "Which city?"


@pytest.mark.asyncio
async def test_waiting_resume_target_excludes_input_admitted_after_continue(tmp_path):
    batches = [
        FakeBatch("initial"),
        FakeBatch("a", text_parts=[FakeTextPart("a", "message_text", "A")]),
        FakeBatch("late", text_parts=[FakeTextPart("l", "message_text", "late")]),
    ]
    service, repositories, _ = make_service(tmp_path, batches, max_batches=1)
    active = await start(service)
    await set_waiting(repositories, active)
    await service.control_service.request_pause(
        session_id="session",
        idempotency_key="pause-late",
        source_client_type="test",
        source_message_ref={"message_id": 50},
    )
    await service.admit_committed_batch("a", session_id="session")
    cont = await service.control_service.request_continue(
        session_id="session",
        idempotency_key="continue-before-late",
        source_client_type="test",
        source_message_ref={"message_id": 51},
    )
    assert service.control_service.resume_input_target(cont.command) == 1
    late = await service.admit_committed_batch("late", session_id="session")
    assert late.action == InputAdmissionAction.QUEUED_PAUSED

    outcome = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.RESUME,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert outcome.applied_input_batch_ids == ("a",)
    assert "late" not in active.applied_input_batch_ids
    row = next(
        row for row in await repositories.inbox.list_for_cycle(active.cycle_id)
        if row.input_batch_id == "late"
    )
    assert row.state.value == "queued"
    assert (await repositories.sessions.get("session")).cycle_status == CycleStatus.RUNNING


@pytest.mark.asyncio
async def test_continue_observes_durable_pause_allocated_before_pause_requested(
    tmp_path, monkeypatch
):
    service, repositories, _ = make_service(tmp_path, [FakeBatch("initial")])
    active = await start(service)
    allocated = asyncio.Event()
    release_pause = asyncio.Event()
    real_allocate = repositories.controls.allocate

    async def barrier_allocate(command):
        result = await real_allocate(command)
        if (
            command.command == ControlCommandType.PAUSE
            and command.idempotency_key == "barrier-pause"
        ):
            allocated.set()
            await release_pause.wait()
        return result

    monkeypatch.setattr(repositories.controls, "allocate", barrier_allocate)
    pause_task = asyncio.create_task(
        service.control_service.request_pause(
            session_id="session",
            idempotency_key="barrier-pause",
            source_client_type="test",
            source_message_ref={"message_id": 60},
        )
    )
    await allocated.wait()
    state_during_barrier = await repositories.sessions.get("session")
    assert state_during_barrier.cycle_status == CycleStatus.RUNNING
    pause_record = await repositories.controls.get_by_idempotency_key(
        "session", "barrier-pause"
    )
    assert pause_record is not None and pause_record.state == ControlState.QUEUED

    cont = await service.control_service.request_continue(
        session_id="session",
        idempotency_key="barrier-continue",
        source_client_type="test",
        source_message_ref={"message_id": 61},
    )
    assert cont.command.state == ControlState.ACKNOWLEDGED
    assert pause_record.sequence_number < cont.command.sequence_number

    release_pause.set()
    pause = await pause_task
    assert pause.command.state == ControlState.ACKNOWLEDGED
    outcome = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.BEFORE_LLM,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert outcome.action != CheckpointAction.PAUSE
    snapshot = await repositories.snapshots.get(active.cycle_id)
    state = await repositories.sessions.get("session")
    rows = await repositories.controls.list_for_session("session")
    assert snapshot.status == CycleStatus.RUNNING
    assert snapshot.pause_reason is None
    assert active.cycle_id == "cycle-a"
    assert [row.sequence_number for row in rows] == [1, 2]
    assert all(row.state == ControlState.APPLIED for row in rows)
    assert state.applied_control_sequence == state.pending_control_sequence == 2
