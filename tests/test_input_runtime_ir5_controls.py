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
    ControlState,
    CycleStatus,
    InputAdmissionAction,
    InputAdmissionService,
    InputRuntimeConfigType,
    create_filesystem_input_runtime_repositories,
)
from src.runtime import ActiveAgentCycle
from src.storage import StorageConfigType


NOW = datetime(2026, 8, 7, 9, 0, tzinfo=timezone.utc)


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
    source_event_ids: tuple[str, ...] = ("evt_" + "1" * 32,)
    content_fingerprint: str = "sha256:" + "2" * 64
    committed_at: datetime = NOW
    continuation_of_batch_id: str | None = None
    correction_of_batch_id: str | None = None
    artifact_manifest: object = field(default_factory=lambda: SimpleNamespace(items=()))

    def model_dump_json(self) -> str:
        return "x" * self.payload_size


class Reader:
    def __init__(self, *batches: FakeBatch):
        self.batches = {item.input_batch_id: item for item in batches}

    async def get_committed(self, input_batch_id: str):
        return self.batches[input_batch_id]


class Wake:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def wake(self, session_id: str, *, cycle_id: str) -> bool:
        self.calls.append((session_id, cycle_id))
        return True


def active_cycle(cycle_id: str) -> ActiveAgentCycle:
    return ActiveAgentCycle(
        cycle_id=cycle_id,
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


def make_service(tmp_path, batches: list[FakeBatch], *, max_batches: int = 20):
    repositories = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
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


async def start_cycle(service: InputAdmissionService):
    initial = await service.admit_committed_batch("initial", session_id="session")
    active = active_cycle(initial.target_cycle_id)
    outcome = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.RESUME,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert outcome.action in {CheckpointAction.CONTINUE, CheckpointAction.INPUT_APPLIED}
    return initial, active


async def persist_status(repositories, active, status: CycleStatus, **snapshot_updates):
    state = await repositories.sessions.get("session")
    await repositories.sessions.compare_and_swap(
        state.revision,
        state.model_copy(
            update={
                "cycle_status": status,
                "revision": state.revision + 1,
                "updated_at": NOW,
            }
        ),
    )
    snapshot = await repositories.snapshots.get(active.cycle_id)
    updates = {
        "status": status,
        "snapshot_revision": snapshot.snapshot_revision + 1,
        "updated_at": NOW,
        **snapshot_updates,
    }
    await repositories.snapshots.compare_and_swap(
        snapshot.snapshot_revision,
        snapshot.model_copy(update=updates),
    )


@pytest.mark.asyncio
async def test_concurrent_control_sequence_allocation_is_unique_and_monotonic(tmp_path):
    service, repositories, _ = make_service(tmp_path, [FakeBatch("initial")])
    _, active = await start_cycle(service)

    async def pause(index: int):
        return await service.control_service.request_pause(
            session_id="session",
            idempotency_key=f"stop-{index}",
            source_client_type="test",
            source_message_ref={"message_id": index},
        )

    async def cont(index: int):
        return await service.control_service.request_continue(
            session_id="session",
            idempotency_key=f"continue-{index}",
            source_client_type="test",
            source_message_ref={"message_id": 100 + index},
        )

    results = await asyncio.gather(
        pause(1), cont(1), pause(2), cont(2), pause(3), cont(3)
    )
    rows = await repositories.controls.list_for_session("session")
    sequences = [row.sequence_number for row in rows]
    assert sequences == list(range(1, len(rows) + 1))
    assert len({row.control_id for row in rows}) == len(rows)
    assert {item.command.control_id for item in results} == {
        row.control_id for row in rows
    }
    state = await repositories.sessions.get("session")
    assert state.pending_control_sequence == len(rows)
    assert state.applied_control_sequence <= state.pending_control_sequence
    assert active.cycle_id == "cycle-a"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["pause", "continue"])
async def test_duplicate_control_delivery_reuses_same_command(tmp_path, kind):
    service, repositories, _ = make_service(tmp_path, [FakeBatch("initial")])
    _, active = await start_cycle(service)
    if kind == "continue":
        await service.control_service.request_pause(
            session_id="session",
            idempotency_key="pause-first",
            source_client_type="test",
            source_message_ref={"message_id": 1},
        )
        await service.checkpoint_service.run_checkpoint(
            checkpoint=CheckpointName.BEFORE_LLM,
            active_cycle=active,
            desired_status=CycleStatus.RUNNING,
        )
        request = service.control_service.request_continue
    else:
        request = service.control_service.request_pause

    first = await request(
        session_id="session",
        idempotency_key=f"dup-{kind}",
        source_client_type="telegram",
        source_message_ref={"chat_id": 1, "message_id": 77},
    )
    second = await request(
        session_id="session",
        idempotency_key=f"dup-{kind}",
        source_client_type="telegram",
        source_message_ref={"chat_id": 1, "message_id": 77},
    )
    assert second.command.control_id == first.command.control_id
    assert second.command.sequence_number == first.command.sequence_number
    matching = [
        row for row in await repositories.controls.list_for_session("session")
        if row.idempotency_key == f"dup-{kind}"
    ]
    assert len(matching) == 1


@pytest.mark.asyncio
async def test_pause_checkpoint_is_snapshot_first_and_preserves_context(tmp_path):
    service, repositories, _ = make_service(tmp_path, [FakeBatch("initial")])
    _, active = await start_cycle(service)
    before = await repositories.snapshots.get(active.cycle_id)
    result = await service.control_service.request_pause(
        session_id="session",
        idempotency_key="stop-1",
        source_client_type="telegram",
        source_message_ref={"chat_id": 1, "message_id": 10},
        reason="user_stop",
    )
    assert result.command.state == ControlState.ACKNOWLEDGED
    state = await repositories.sessions.get("session")
    assert state.cycle_status == CycleStatus.PAUSE_REQUESTED

    checkpoint = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.BEFORE_LLM,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert checkpoint.action == CheckpointAction.PAUSE
    snapshot = await repositories.snapshots.get(active.cycle_id)
    state = await repositories.sessions.get("session")
    control = await repositories.controls.get(result.command.control_id)
    assert snapshot.status == CycleStatus.PAUSED_BY_USER
    assert snapshot.pause_reason == "user_stop"
    assert snapshot.original_input_batch_id == before.original_input_batch_id
    assert snapshot.active_context_revision_id == before.active_context_revision_id
    assert snapshot.messages_for_llm == before.messages_for_llm
    assert state.cycle_status == CycleStatus.PAUSED_BY_USER
    assert control.state == ControlState.APPLIED
    assert state.applied_control_sequence == control.sequence_number


@pytest.mark.asyncio
async def test_paused_input_is_fifo_queued_without_wake_or_auto_resume(tmp_path):
    batches = [
        FakeBatch("initial"),
        FakeBatch("a", text_parts=[FakeTextPart("a", "message_text", "A")]),
        FakeBatch("b", text_parts=[FakeTextPart("b", "message_text", "B")]),
    ]
    service, repositories, wake = make_service(tmp_path, batches)
    _, active = await start_cycle(service)
    await service.control_service.request_pause(
        session_id="session",
        idempotency_key="stop",
        source_client_type="test",
        source_message_ref={"message_id": 1},
    )
    await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.BEFORE_LLM,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    wake.calls.clear()

    one = await service.admit_committed_batch("a", session_id="session")
    two = await service.admit_committed_batch("b", session_id="session")
    assert one.action == InputAdmissionAction.QUEUED_PAUSED
    assert two.action == InputAdmissionAction.QUEUED_PAUSED
    assert one.should_wake_runner is False
    assert two.should_wake_runner is False
    assert wake.calls == []
    rows = await repositories.inbox.list_for_cycle(active.cycle_id)
    assert [row.input_batch_id for row in rows] == ["a", "b"]
    assert [row.state.value for row in rows] == ["queued", "queued"]
    state = await repositories.sessions.get("session")
    assert state.cycle_status == CycleStatus.PAUSED_BY_USER


@pytest.mark.asyncio
async def test_continue_same_cycle_drains_precontinue_target_before_late_input(tmp_path):
    batches = [
        FakeBatch("initial"),
        FakeBatch("a", text_parts=[FakeTextPart("a", "message_text", "A")]),
        FakeBatch("b", text_parts=[FakeTextPart("b", "message_text", "B")]),
        FakeBatch("c", text_parts=[FakeTextPart("c", "message_text", "C")]),
        FakeBatch("late", text_parts=[FakeTextPart("l", "message_text", "late")]),
    ]
    service, repositories, _ = make_service(tmp_path, batches, max_batches=1)
    _, active = await start_cycle(service)
    await service.control_service.request_pause(
        session_id="session",
        idempotency_key="stop",
        source_client_type="test",
        source_message_ref={"message_id": 1},
    )
    await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.BEFORE_LLM,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    for batch_id in ("a", "b", "c"):
        await service.admit_committed_batch(batch_id, session_id="session")

    resumed = await service.control_service.request_continue(
        session_id="session",
        idempotency_key="continue",
        source_client_type="test",
        source_message_ref={"message_id": 2},
    )
    assert resumed.command.target_cycle_id == active.cycle_id
    assert service.control_service.resume_input_target(resumed.command) == 3
    late = await service.admit_committed_batch("late", session_id="session")
    assert late.action == InputAdmissionAction.QUEUED_PAUSED

    outcome = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.RESUME,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert outcome.action == CheckpointAction.INPUT_APPLIED
    assert outcome.applied_input_batch_ids == ("a", "b", "c")
    assert active.cycle_id == "cycle-a"
    assert active.applied_through_cycle_sequence == 3
    assert "late" not in active.applied_input_batch_ids
    rows = await repositories.inbox.list_for_cycle(active.cycle_id)
    late_row = next(row for row in rows if row.input_batch_id == "late")
    assert late_row.state.value == "queued"
    state = await repositories.sessions.get("session")
    assert state.cycle_status == CycleStatus.RUNNING


@pytest.mark.asyncio
async def test_rapid_pause_continue_is_reduced_without_phantom_pause(tmp_path):
    service, repositories, _ = make_service(tmp_path, [FakeBatch("initial")])
    _, active = await start_cycle(service)
    pause = await service.control_service.request_pause(
        session_id="session",
        idempotency_key="pause",
        source_client_type="test",
        source_message_ref={"message_id": 1},
    )
    cont = await service.control_service.request_continue(
        session_id="session",
        idempotency_key="continue",
        source_client_type="test",
        source_message_ref={"message_id": 2},
    )
    assert pause.command.sequence_number < cont.command.sequence_number
    before = await repositories.snapshots.get(active.cycle_id)
    assert before.status == CycleStatus.RUNNING

    outcome = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.BEFORE_LLM,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert outcome.action != CheckpointAction.PAUSE
    snapshot = await repositories.snapshots.get(active.cycle_id)
    state = await repositories.sessions.get("session")
    assert snapshot.status == CycleStatus.RUNNING
    assert snapshot.pause_reason is None
    assert state.cycle_status == CycleStatus.RUNNING
    assert state.applied_control_sequence == state.pending_control_sequence == 2


@pytest.mark.asyncio
async def test_waiting_pause_preserves_question_and_continue_does_not_answer_it(tmp_path):
    service, repositories, _ = make_service(tmp_path, [FakeBatch("initial")])
    _, active = await start_cycle(service)
    await persist_status(
        repositories,
        active,
        CycleStatus.WAITING_USER,
        waiting_question="Which city?",
    )
    active.waiting_question = "Which city?"

    paused = await service.control_service.request_pause(
        session_id="session",
        idempotency_key="pause-waiting",
        source_client_type="test",
        source_message_ref={"message_id": 1},
    )
    assert paused.command.state == ControlState.APPLIED
    snapshot = await repositories.snapshots.get(active.cycle_id)
    assert snapshot.status == CycleStatus.PAUSED_BY_USER
    assert snapshot.waiting_question == "Which city?"

    await service.control_service.request_continue(
        session_id="session",
        idempotency_key="continue-waiting",
        source_client_type="test",
        source_message_ref={"message_id": 2},
    )
    outcome = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.RESUME,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert outcome.action == CheckpointAction.WAIT
    snapshot = await repositories.snapshots.get(active.cycle_id)
    assert snapshot.status == CycleStatus.WAITING_USER
    assert snapshot.waiting_question == "Which city?"
    assert len(snapshot.messages_for_llm) == 2


@pytest.mark.asyncio
async def test_continue_running_and_stop_idle_are_durable_noops(tmp_path):
    service, repositories, _ = make_service(tmp_path, [FakeBatch("initial")])
    _, active = await start_cycle(service)
    running = await service.control_service.request_continue(
        session_id="session",
        idempotency_key="continue-running",
        source_client_type="test",
        source_message_ref={"message_id": 1},
    )
    assert running.command.state == ControlState.REJECTED
    assert running.command.rejection_code == "already_running"

    await service.control_service.request_reset(
        session_id="session",
        idempotency_key="reset",
        source_client_type="test",
        source_message_ref={"message_id": 2},
    )
    stopped = await service.control_service.request_pause(
        session_id="session",
        idempotency_key="stop-idle",
        source_client_type="test",
        source_message_ref={"message_id": 3},
    )
    assert stopped.command.state == ControlState.REJECTED
    assert stopped.command.rejection_code == "no_active_cycle"
    state = await repositories.sessions.get("session")
    assert state.cycle_status == CycleStatus.IDLE
    assert state.active_cycle_id is None
    assert active.cycle_id == "cycle-a"


@pytest.mark.asyncio
async def test_reset_advances_generation_once_and_fences_old_cycle(tmp_path):
    batches = [
        FakeBatch("initial"),
        FakeBatch("queued", text_parts=[FakeTextPart("q", "message_text", "queued")]),
    ]
    service, repositories, _ = make_service(tmp_path, batches)
    initial, active = await start_cycle(service)
    queued = await service.admit_committed_batch("queued", session_id="session")
    assert queued.action == InputAdmissionAction.QUEUED_RUNNING

    first = await service.control_service.request_reset(
        session_id="session",
        idempotency_key="reset-1",
        source_client_type="telegram",
        source_message_ref={"chat_id": 1, "message_id": 50},
    )
    state = await repositories.sessions.get("session")
    assert state.generation == 1
    assert state.active_cycle_id is None
    assert state.cycle_status == CycleStatus.IDLE
    assert first.command.state == ControlState.APPLIED
    assert first.command.generation == 0

    duplicate = await service.control_service.request_reset(
        session_id="session",
        idempotency_key="reset-1",
        source_client_type="telegram",
        source_message_ref={"chat_id": 1, "message_id": 50},
    )
    assert duplicate.command.control_id == first.command.control_id
    assert (await repositories.sessions.get("session")).generation == 1

    inbox = await repositories.inbox.list_for_cycle(active.cycle_id)
    assert all(row.state.value == "cancelled" for row in inbox)
    old_admission = await repositories.admissions.get_by_input_batch_id("queued")
    assert old_admission.state.value == "cancelled"
    old_snapshot = await repositories.snapshots.get(active.cycle_id)
    assert old_snapshot.status == CycleStatus.CANCELLED

    stale = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.BEFORE_LLM,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert stale.action == CheckpointAction.INTERRUPT
    assert "authority" in (stale.reason_code or "") or "generation" in (stale.reason_code or "")
    assert initial.admitted_generation == 0


@pytest.mark.asyncio
async def test_independent_resets_advance_distinct_generations(tmp_path):
    service, repositories, _ = make_service(tmp_path, [FakeBatch("initial")])
    await start_cycle(service)
    one = await service.control_service.request_reset(
        session_id="session",
        idempotency_key="reset-1",
        source_client_type="test",
        source_message_ref={"message_id": 1},
    )
    two = await service.control_service.request_reset(
        session_id="session",
        idempotency_key="reset-2",
        source_client_type="test",
        source_message_ref={"message_id": 2},
    )
    assert one.command.control_id != two.command.control_id
    assert one.command.sequence_number + 1 == two.command.sequence_number
    assert (await repositories.sessions.get("session")).generation == 2


@pytest.mark.asyncio
async def test_control_record_first_publication_repairs_missing_pending_watermark(
    tmp_path, monkeypatch
):
    service, repositories, _ = make_service(tmp_path, [FakeBatch("initial")])
    await start_cycle(service)
    import src.input_runtime._filesystem_identity_recovery_session as module

    real_write = module.atomic_write_model
    failed = False

    def fail_state_once(path, model):
        nonlocal failed
        if not failed and path.name == "state.json":
            failed = True
            raise OSError("state write fault")
        return real_write(path, model)

    monkeypatch.setattr(module, "atomic_write_model", fail_state_once)
    with pytest.raises(OSError, match="state write fault"):
        await service.control_service.request_pause(
            session_id="session",
            idempotency_key="fault-stop",
            source_client_type="test",
            source_message_ref={"message_id": 91},
        )
    record = await repositories.controls.get_by_idempotency_key(
        "session", "fault-stop"
    )
    assert record is not None
    state = await repositories.sessions.get("session")
    assert state.pending_control_sequence < record.sequence_number

    monkeypatch.setattr(module, "atomic_write_model", real_write)
    repaired = await service.control_service.request_pause(
        session_id="session",
        idempotency_key="fault-stop",
        source_client_type="test",
        source_message_ref={"message_id": 91},
    )
    assert repaired.command.control_id == record.control_id
    assert repaired.command.sequence_number == record.sequence_number
    state = await repositories.sessions.get("session")
    assert state.pending_control_sequence >= record.sequence_number


@pytest.mark.asyncio
async def test_pause_effect_survives_control_apply_mark_failure(tmp_path, monkeypatch):
    service, repositories, _ = make_service(tmp_path, [FakeBatch("initial")])
    _, active = await start_cycle(service)
    pause = await service.control_service.request_pause(
        session_id="session",
        idempotency_key="pause-fault",
        source_client_type="test",
        source_message_ref={"message_id": 1},
    )
    real_apply = repositories.controls.apply
    failed = False

    async def fail_once(control_id, *, applied_at):
        nonlocal failed
        if control_id == pause.command.control_id and not failed:
            failed = True
            raise OSError("apply marker fault")
        return await real_apply(control_id, applied_at=applied_at)

    monkeypatch.setattr(repositories.controls, "apply", fail_once)
    with pytest.raises(OSError, match="apply marker fault"):
        await service.checkpoint_service.run_checkpoint(
            checkpoint=CheckpointName.BEFORE_LLM,
            active_cycle=active,
            desired_status=CycleStatus.RUNNING,
        )
    snapshot = await repositories.snapshots.get(active.cycle_id)
    assert snapshot.status == CycleStatus.PAUSED_BY_USER

    monkeypatch.setattr(repositories.controls, "apply", real_apply)
    retried = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.BEFORE_LLM,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert retried.action == CheckpointAction.PAUSE
    rows = [
        row for row in await repositories.controls.list_for_session("session")
        if row.idempotency_key == "pause-fault"
    ]
    assert len(rows) == 1
    assert rows[0].state == ControlState.APPLIED
