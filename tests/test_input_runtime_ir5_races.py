from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.agent.protocol import AgentAction
from src.api.session_reset import reset_runtime_session
from src.input_runtime import (
    CheckpointAction,
    CheckpointName,
    ControlState,
    CycleStatus,
    InputAdmissionService,
    InputRuntimeConfigType,
    create_filesystem_input_runtime_repositories,
)
from src.mcp.input_runtime_checkpoint_hardening import InputRuntimeCheckpointHardeningMixin
from src.mcp.input_runtime_checkpoints import InputRuntimeCheckpointMixin
from src.mcp.input_runtime_controls import InputRuntimeControlMixin, _ControlUnwind
from src.runtime import ActiveAgentCycle, SessionExecutionCoordinator
from src.storage import StorageConfigType


NOW = datetime(2026, 8, 7, 10, 0, tzinfo=timezone.utc)


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
    source_event_ids: tuple[str, ...] = ("evt_" + "3" * 32,)
    content_fingerprint: str = "sha256:" + "4" * 64
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
    async def wake(self, session_id: str, *, cycle_id: str) -> bool:
        return True


def make_service(tmp_path, batches=None):
    batches = batches or [FakeBatch("initial")]
    repositories = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
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


def cycle() -> ActiveAgentCycle:
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
    active = cycle()
    assert admission.target_cycle_id == active.cycle_id
    await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.RESUME,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    return active


class BlockingLLMBase:
    def __init__(self):
        self.llm_entered = asyncio.Event()
        self.llm_release = asyncio.Event()
        self.llm_calls = 0

    async def _call_main_llm_with_context_recovery(self, **kwargs):
        self.llm_calls += 1
        self.llm_entered.set()
        await self.llm_release.wait()
        return (
            {
                "content": AgentAction(
                    status="done",
                    action="answer",
                    final_answer="candidate",
                ).model_dump_json(),
                "tool_calls": [],
            },
            kwargs["active_cycle"].messages_for_llm,
        )


class ControlLLMHarness(
    InputRuntimeControlMixin,
    InputRuntimeCheckpointHardeningMixin,
    InputRuntimeCheckpointMixin,
    BlockingLLMBase,
):
    pass


@pytest.mark.asyncio
async def test_stop_during_actual_llm_waits_for_attempt_then_pauses_before_next_block(tmp_path):
    service, repositories = make_service(tmp_path)
    active = await start(service)
    harness = ControlLLMHarness()

    task = asyncio.create_task(
        harness._call_main_llm_with_context_recovery(
            active_cycle=active,
            state=SimpleNamespace(),
            session_id="session",
            progress_callback=None,
            tools=[],
            context="",
            include_iteration_runtime=False,
        )
    )
    await harness.llm_entered.wait()
    pause = await service.control_service.request_pause(
        session_id="session",
        idempotency_key="stop-during-llm",
        source_client_type="test",
        source_message_ref={"message_id": 10},
    )
    assert pause.command.state == ControlState.ACKNOWLEDGED
    assert not task.done()
    assert (await repositories.sessions.get("session")).cycle_status == CycleStatus.PAUSE_REQUESTED

    harness.llm_release.set()
    with pytest.raises(_ControlUnwind) as unwind:
        await task
    assert unwind.value.action == CheckpointAction.PAUSE
    assert harness.llm_calls == 1
    snapshot = await repositories.snapshots.get(active.cycle_id)
    assert snapshot.status == CycleStatus.PAUSED_BY_USER
    assert (await repositories.sessions.get("session")).cycle_status == CycleStatus.PAUSED_BY_USER


@pytest.mark.asyncio
async def test_stop_inside_multitool_block_completes_all_tool_results_before_pause(tmp_path):
    service, repositories = make_service(tmp_path)
    active = await start(service)
    harness = ControlLLMHarness()
    active.messages_for_llm.append(
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "call-1", "type": "function", "function": {"name": "one"}},
                {"id": "call-2", "type": "function", "function": {"name": "two"}},
            ],
        }
    )
    tool_block_entered = asyncio.Event()
    tool_block_release = asyncio.Event()

    async def execute_complete_tool_block():
        active.messages_for_llm.append(
            {"role": "tool", "tool_call_id": "call-1", "content": "one"}
        )
        tool_block_entered.set()
        await tool_block_release.wait()
        active.messages_for_llm.append(
            {"role": "tool", "tool_call_id": "call-2", "content": "two"}
        )

    block_task = asyncio.create_task(execute_complete_tool_block())
    await tool_block_entered.wait()
    pause = await service.control_service.request_pause(
        session_id="session",
        idempotency_key="stop-during-tools",
        source_client_type="test",
        source_message_ref={"message_id": 20},
    )
    assert pause.command.state == ControlState.ACKNOWLEDGED
    tool_block_release.set()
    await block_task
    assert InputRuntimeCheckpointMixin._last_block_is_complete_tool_block(
        active.messages_for_llm
    )

    # The next LLM request first crosses CP-AFTER-TOOL-BLOCK.  It must unwind
    # there, so BlockingLLMBase is never entered a second time.
    with pytest.raises(_ControlUnwind):
        await harness._call_main_llm_with_context_recovery(
            active_cycle=active,
            state=SimpleNamespace(),
            session_id="session",
            progress_callback=None,
            tools=[],
            context="",
            include_iteration_runtime=False,
        )
    assert harness.llm_calls == 0
    snapshot = await repositories.snapshots.get(active.cycle_id)
    assert snapshot.status == CycleStatus.PAUSED_BY_USER
    roles = [item.get("role") for item in snapshot.messages_for_llm[-3:]]
    assert roles == ["assistant", "tool", "tool"]
    assert {
        item.get("tool_call_id")
        for item in snapshot.messages_for_llm
        if item.get("role") == "tool"
    } >= {"call-1", "call-2"}


@pytest.mark.asyncio
async def test_pause_then_continue_while_llm_blocked_neutralizes_pause_without_second_runner(tmp_path):
    service, repositories = make_service(tmp_path)
    active = await start(service)
    harness = ControlLLMHarness()
    task = asyncio.create_task(
        harness._call_main_llm_with_context_recovery(
            active_cycle=active,
            state=SimpleNamespace(),
            session_id="session",
            progress_callback=None,
            tools=[],
            context="",
            include_iteration_runtime=False,
        )
    )
    await harness.llm_entered.wait()
    pause = await service.control_service.request_pause(
        session_id="session",
        idempotency_key="rapid-stop",
        source_client_type="test",
        source_message_ref={"message_id": 30},
    )
    cont = await service.control_service.request_continue(
        session_id="session",
        idempotency_key="rapid-continue",
        source_client_type="test",
        source_message_ref={"message_id": 31},
    )
    assert pause.command.sequence_number < cont.command.sequence_number
    assert not task.done()
    harness.llm_release.set()
    response, _ = await task
    assert response["content"]
    snapshot = await repositories.snapshots.get(active.cycle_id)
    state = await repositories.sessions.get("session")
    assert snapshot.status == CycleStatus.RUNNING
    assert snapshot.pause_reason is None
    assert state.cycle_status == CycleStatus.RUNNING
    assert state.applied_control_sequence == state.pending_control_sequence
    assert harness.llm_calls == 1


@pytest.mark.asyncio
async def test_pause_then_input_checkpoint_prioritizes_pause_and_leaves_input_queued(tmp_path):
    batches = [
        FakeBatch("initial"),
        FakeBatch("addition", text_parts=[FakeTextPart("p", "message_text", "later")]),
    ]
    service, repositories = make_service(tmp_path, batches)
    active = await start(service)
    await service.control_service.request_pause(
        session_id="session",
        idempotency_key="pause-before-input",
        source_client_type="test",
        source_message_ref={"message_id": 40},
    )
    addition = await service.admit_committed_batch("addition", session_id="session")
    assert addition.should_start_runner is False
    outcome = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.BEFORE_LLM,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert outcome.action == CheckpointAction.PAUSE
    row = (await repositories.inbox.list_for_cycle(active.cycle_id))[0]
    assert row.input_batch_id == "addition"
    assert row.state.value == "queued"
    assert "addition" not in active.applied_input_batch_ids


@pytest.mark.asyncio
async def test_pause_ack_failure_repairs_same_control_without_new_sequence(tmp_path, monkeypatch):
    service, repositories = make_service(tmp_path)
    await start(service)
    real_ack = repositories.controls.acknowledge
    failed = False

    async def fail_once(control_id, *, acknowledged_at):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("ack fault")
        return await real_ack(control_id, acknowledged_at=acknowledged_at)

    monkeypatch.setattr(repositories.controls, "acknowledge", fail_once)
    with pytest.raises(OSError, match="ack fault"):
        await service.control_service.request_pause(
            session_id="session",
            idempotency_key="pause-ack-fault",
            source_client_type="test",
            source_message_ref={"message_id": 50},
        )
    state = await repositories.sessions.get("session")
    assert state.cycle_status == CycleStatus.PAUSE_REQUESTED
    record = await repositories.controls.get_by_idempotency_key(
        "session", "pause-ack-fault"
    )
    assert record is not None and record.state == ControlState.QUEUED

    monkeypatch.setattr(repositories.controls, "acknowledge", real_ack)
    repaired = await service.control_service.request_pause(
        session_id="session",
        idempotency_key="pause-ack-fault",
        source_client_type="test",
        source_message_ref={"message_id": 50},
    )
    assert repaired.command.control_id == record.control_id
    assert repaired.command.sequence_number == record.sequence_number
    assert repaired.command.state == ControlState.ACKNOWLEDGED
    rows = await repositories.controls.list_for_session("session")
    assert len([row for row in rows if row.idempotency_key == "pause-ack-fault"]) == 1


@pytest.mark.asyncio
async def test_continue_effect_survives_apply_marker_failure_without_second_resume(tmp_path, monkeypatch):
    service, repositories = make_service(tmp_path)
    active = await start(service)
    await service.control_service.request_pause(
        session_id="session",
        idempotency_key="pause",
        source_client_type="test",
        source_message_ref={"message_id": 60},
    )
    await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.BEFORE_LLM,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    cont = await service.control_service.request_continue(
        session_id="session",
        idempotency_key="continue-fault",
        source_client_type="test",
        source_message_ref={"message_id": 61},
    )
    real_apply = repositories.controls.apply
    failed = False

    async def fail_once(control_id, *, applied_at):
        nonlocal failed
        if control_id == cont.command.control_id and not failed:
            failed = True
            raise OSError("continue marker fault")
        return await real_apply(control_id, applied_at=applied_at)

    monkeypatch.setattr(repositories.controls, "apply", fail_once)
    with pytest.raises(OSError, match="continue marker fault"):
        await service.checkpoint_service.run_checkpoint(
            checkpoint=CheckpointName.RESUME,
            active_cycle=active,
            desired_status=CycleStatus.RUNNING,
        )
    snapshot = await repositories.snapshots.get(active.cycle_id)
    assert snapshot.status == CycleStatus.RUNNING

    monkeypatch.setattr(repositories.controls, "apply", real_apply)
    retried = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.RESUME,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert retried.action != CheckpointAction.PAUSE
    rows = [
        row for row in await repositories.controls.list_for_session("session")
        if row.idempotency_key == "continue-fault"
    ]
    assert len(rows) == 1
    assert rows[0].state == ControlState.APPLIED


@pytest.mark.asyncio
async def test_reset_generation_is_not_repeated_when_old_generation_cleanup_faults(tmp_path, monkeypatch):
    batches = [
        FakeBatch("initial"),
        FakeBatch("queued", text_parts=[FakeTextPart("q", "message_text", "queued")]),
    ]
    service, repositories = make_service(tmp_path, batches)
    await start(service)
    await service.admit_committed_batch("queued", session_id="session")
    real_cancel = repositories.inbox.cancel_generation
    failed = False

    async def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("cleanup fault")
        return await real_cancel(*args, **kwargs)

    monkeypatch.setattr(repositories.inbox, "cancel_generation", fail_once)
    with pytest.raises(OSError, match="cleanup fault"):
        await service.control_service.request_reset(
            session_id="session",
            idempotency_key="reset-cleanup-fault",
            source_client_type="test",
            source_message_ref={"message_id": 70},
        )
    state = await repositories.sessions.get("session")
    assert state.generation == 1
    record = await repositories.controls.get_by_idempotency_key(
        "session", "reset-cleanup-fault"
    )
    assert record is not None

    monkeypatch.setattr(repositories.inbox, "cancel_generation", real_cancel)
    repaired = await service.control_service.request_reset(
        session_id="session",
        idempotency_key="reset-cleanup-fault",
        source_client_type="test",
        source_message_ref={"message_id": 70},
    )
    assert repaired.command.control_id == record.control_id
    assert (await repositories.sessions.get("session")).generation == 1
    assert repaired.command.state == ControlState.APPLIED


@pytest.mark.asyncio
async def test_reset_visible_before_terminal_checkpoint_suppresses_old_candidate(tmp_path):
    service, repositories = make_service(tmp_path)
    active = await start(service)
    active.messages_for_llm.append(
        {
            "role": "assistant",
            "content": AgentAction(
                status="done",
                action="answer",
                final_answer="stale final",
            ).model_dump_json(),
        }
    )
    terminal_ready = asyncio.Event()
    release_checkpoint = asyncio.Event()

    async def terminal_candidate():
        terminal_ready.set()
        await release_checkpoint.wait()
        return await service.checkpoint_service.run_checkpoint(
            checkpoint=CheckpointName.BEFORE_TERMINAL_COMMIT,
            active_cycle=active,
            desired_status=CycleStatus.DONE,
        )

    task = asyncio.create_task(terminal_candidate())
    await terminal_ready.wait()
    await service.control_service.request_reset(
        session_id="session",
        idempotency_key="reset-before-terminal",
        source_client_type="test",
        source_message_ref={"message_id": 80},
    )
    release_checkpoint.set()
    outcome = await task
    assert outcome.action == CheckpointAction.INTERRUPT
    state = await repositories.sessions.get("session")
    assert state.generation == 1
    assert state.cycle_status == CycleStatus.IDLE
    assert state.active_cycle_id is None
    snapshot = await repositories.snapshots.get(active.cycle_id)
    assert snapshot.status == CycleStatus.CANCELLED


@pytest.mark.asyncio
async def test_stale_wake_cannot_target_new_cycle_or_generation():
    coordinator = SessionExecutionCoordinator()
    await coordinator.synchronize_generation("session", generation=1)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_new_cycle():
        async with coordinator.admitted_run_lease(
            session_id="session",
            input_batch_id="new-input",
            cycle_id="cycle-new",
            expected_generation=1,
        ) as acquired:
            assert acquired
            entered.set()
            await release.wait()

    task = asyncio.create_task(hold_new_cycle())
    await entered.wait()
    assert await coordinator.wake(
        "session", cycle_id="cycle-old", generation=0
    ) is False
    assert await coordinator.wake(
        "session", cycle_id="cycle-new", generation=0
    ) is False
    assert await coordinator.wake(
        "session", cycle_id="cycle-new", generation=1
    ) is True
    release.set()
    await task


@pytest.mark.asyncio
async def test_reset_waits_for_execution_lease_before_shared_memory_clear(tmp_path):
    service, repositories = make_service(tmp_path)
    await start(service)
    coordinator = SessionExecutionCoordinator()
    entered = asyncio.Event()
    release = asyncio.Event()
    cleared = asyncio.Event()

    async def old_runner():
        async with coordinator.admitted_run_lease(
            session_id="session",
            input_batch_id="initial",
            cycle_id="cycle-a",
            expected_generation=0,
        ) as acquired:
            assert acquired
            entered.set()
            await release.wait()

    runner = asyncio.create_task(old_runner())
    await entered.wait()

    class BatchStore:
        async def cancel_open_drafts(self, **kwargs):
            return []

    fake_api = SimpleNamespace(
        input_admission_service=service,
        input_runtime_repositories=repositories,
        execution_coordinator=coordinator,
        ingress_services=SimpleNamespace(
            batch_store=BatchStore(),
            ingress_service=SimpleNamespace(presentation_coordinator=None),
        ),
        mcp_client=SimpleNamespace(
            clear_session=lambda session_id: cleared.set()
        ),
    )
    reset = asyncio.create_task(
        reset_runtime_session(
            fake_api,
            "session",
            idempotency_key="safe-memory-reset",
            source_client_type="test",
            source_message_ref={"message_id": 90},
        )
    )
    for _ in range(20):
        if (await repositories.sessions.get("session")).generation == 1:
            break
        await asyncio.sleep(0)
    assert (await repositories.sessions.get("session")).generation == 1
    assert not cleared.is_set()
    release.set()
    await runner
    result = await reset
    assert cleared.is_set()
    assert result.generation == 1
