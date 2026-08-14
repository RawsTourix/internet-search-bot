from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.api.input_runtime_controls import request_runtime_continue
from src.core.models import AgentResult, AgentStatus, ClientType
from src.input_runtime import (
    CheckpointAction,
    CheckpointName,
    ControlState,
    CycleStatus,
    InputAdmissionService,
    InputRuntimeConfigType,
    create_filesystem_input_runtime_repositories,
)
from src.runtime import ActiveAgentCycle, SessionExecutionCoordinator
from src.storage import StorageConfigType


NOW = datetime(2026, 8, 7, 10, 30, tzinfo=timezone.utc)


@dataclass
class Batch:
    input_batch_id: str
    session_id: str = "session"
    payload_size: int = 10
    text_parts: list = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    source_event_ids: tuple[str, ...] = ("evt_" + "5" * 32,)
    content_fingerprint: str = "sha256:" + "6" * 64
    committed_at: datetime = NOW
    continuation_of_batch_id: str | None = None
    correction_of_batch_id: str | None = None
    artifact_manifest: object = field(default_factory=lambda: SimpleNamespace(items=()))

    def model_dump_json(self) -> str:
        return "x" * self.payload_size


class Reader:
    async def get_committed(self, input_batch_id: str):
        return Batch(input_batch_id)


class Wake:
    async def wake(self, session_id: str, *, cycle_id: str) -> bool:
        return True


def make_runtime(tmp_path):
    repositories = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )
    service = InputAdmissionService(
        config=InputRuntimeConfigType(),
        repositories=repositories,
        committed_batches=Reader(),
        wake_coordinator=Wake(),
        cycle_id_factory=lambda: "cycle-a",
        clock=lambda: NOW,
        payload_size_resolver=lambda batch: batch.payload_size,
    )
    active = ActiveAgentCycle(
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
    return service, repositories, active


async def start(service, active):
    outcome = await service.admit_committed_batch("initial", session_id="session")
    assert outcome.target_cycle_id == active.cycle_id
    checkpoint = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.RESUME,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert checkpoint.action != CheckpointAction.INTERRUPT


async def set_status(repositories, active, status: CycleStatus):
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
        "pause_reason": None,
        "waiting_question": None,
        "interruption_reason": None,
    }
    if status == CycleStatus.PAUSED_BY_USER:
        updates["pause_reason"] = "test_pause"
    elif status == CycleStatus.WAITING_USER:
        updates["waiting_question"] = "Need an answer"
    elif status == CycleStatus.INTERRUPTED:
        updates["interruption_reason"] = "retryable_interrupt"
    await repositories.snapshots.compare_and_swap(
        snapshot.snapshot_revision,
        snapshot.model_copy(update=updates),
    )


@pytest.mark.asyncio
async def test_continue_waiting_without_answer_is_explicit_noop(tmp_path):
    service, repositories, active = make_runtime(tmp_path)
    await start(service, active)
    await set_status(repositories, active, CycleStatus.WAITING_USER)
    before = await repositories.snapshots.get(active.cycle_id)

    outcome = await service.control_service.request_continue(
        session_id="session",
        idempotency_key="continue-waiting-direct",
        source_client_type="test",
        source_message_ref={"message_id": 1},
    )

    assert outcome.command.state == ControlState.REJECTED
    assert outcome.command.rejection_code == "still_waiting_for_input"
    after = await repositories.snapshots.get(active.cycle_id)
    assert after.status == CycleStatus.WAITING_USER
    assert after.waiting_question == "Need an answer"
    assert after.messages_for_llm == before.messages_for_llm


@pytest.mark.asyncio
async def test_pause_interrupted_cycle_is_durable_and_resumable(tmp_path):
    service, repositories, active = make_runtime(tmp_path)
    await start(service, active)
    await set_status(repositories, active, CycleStatus.INTERRUPTED)

    outcome = await service.control_service.request_pause(
        session_id="session",
        idempotency_key="pause-interrupted",
        source_client_type="test",
        source_message_ref={"message_id": 2},
    )

    assert outcome.command.state == ControlState.APPLIED
    snapshot = await repositories.snapshots.get(active.cycle_id)
    assert snapshot.status == CycleStatus.PAUSED_BY_USER
    assert snapshot.interruption_reason == "retryable_interrupt"
    assert snapshot.pause_reason


@pytest.mark.asyncio
async def test_continue_same_cycle_without_additions_preserves_revision(tmp_path):
    service, repositories, active = make_runtime(tmp_path)
    await start(service, active)
    before = await repositories.snapshots.get(active.cycle_id)
    await service.control_service.request_pause(
        session_id="session",
        idempotency_key="pause-no-additions",
        source_client_type="test",
        source_message_ref={"message_id": 3},
    )
    await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.BEFORE_LLM,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    resumed = await service.control_service.request_continue(
        session_id="session",
        idempotency_key="continue-no-additions",
        source_client_type="test",
        source_message_ref={"message_id": 4},
    )
    outcome = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.RESUME,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )

    after = await repositories.snapshots.get(active.cycle_id)
    assert resumed.command.target_cycle_id == active.cycle_id == "cycle-a"
    assert outcome.action == CheckpointAction.CONTINUE
    assert after.status == CycleStatus.RUNNING
    assert after.active_context_revision_id == before.active_context_revision_id
    assert after.original_input_batch_id == before.original_input_batch_id


@pytest.mark.asyncio
async def test_pause_visible_at_terminal_checkpoint_suppresses_terminal_candidate(tmp_path):
    service, repositories, active = make_runtime(tmp_path)
    await start(service, active)
    await service.control_service.request_pause(
        session_id="session",
        idempotency_key="pause-terminal",
        source_client_type="test",
        source_message_ref={"message_id": 5},
    )

    outcome = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.BEFORE_TERMINAL_COMMIT,
        active_cycle=active,
        desired_status=CycleStatus.DONE,
    )

    assert outcome.action == CheckpointAction.PAUSE
    state = await repositories.sessions.get("session")
    snapshot = await repositories.snapshots.get(active.cycle_id)
    assert state.cycle_status == CycleStatus.PAUSED_BY_USER
    assert snapshot.status == CycleStatus.PAUSED_BY_USER


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        CycleStatus.PAUSED_BY_USER,
        CycleStatus.WAITING_USER,
        CycleStatus.INTERRUPTED,
    ],
)
async def test_reset_from_resumable_states_fences_old_cycle(tmp_path, status):
    service, repositories, active = make_runtime(tmp_path)
    await start(service, active)
    await set_status(repositories, active, status)

    outcome = await service.control_service.request_reset(
        session_id="session",
        idempotency_key=f"reset-{status.value}",
        source_client_type="test",
        source_message_ref={"message_id": status.value},
    )

    state = await repositories.sessions.get("session")
    snapshot = await repositories.snapshots.get(active.cycle_id)
    assert outcome.command.state == ControlState.APPLIED
    assert state.generation == 1
    assert state.active_cycle_id is None
    assert state.cycle_status == CycleStatus.IDLE
    assert snapshot.status == CycleStatus.CANCELLED


@pytest.mark.asyncio
async def test_compatibility_result_mapping_cannot_overwrite_pause_or_reset(tmp_path):
    service, repositories, active = make_runtime(tmp_path)
    await start(service, active)
    await service.control_service.request_pause(
        session_id="session",
        idempotency_key="pause-result-map",
        source_client_type="test",
        source_message_ref={"message_id": 6},
    )
    await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.BEFORE_LLM,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    paused = await service.record_cycle_status(
        session_id="session",
        cycle_id=active.cycle_id,
        status=CycleStatus.DONE,
    )
    assert paused.cycle_status == CycleStatus.PAUSED_BY_USER
    assert (await repositories.snapshots.get(active.cycle_id)).status == CycleStatus.PAUSED_BY_USER

    await service.control_service.request_reset(
        session_id="session",
        idempotency_key="reset-result-map",
        source_client_type="test",
        source_message_ref={"message_id": 7},
    )
    stale = await service.record_cycle_status(
        session_id="session",
        cycle_id=active.cycle_id,
        status=CycleStatus.DONE,
    )
    assert stale.generation == 1
    assert stale.cycle_status == CycleStatus.IDLE
    assert stale.active_cycle_id is None


@pytest.mark.asyncio
async def test_reset_supersedes_pending_pause_in_old_generation(tmp_path):
    service, repositories, active = make_runtime(tmp_path)
    await start(service, active)
    pause = await service.control_service.request_pause(
        session_id="session",
        idempotency_key="pending-pause",
        source_client_type="test",
        source_message_ref={"message_id": 8},
    )
    assert pause.command.state == ControlState.ACKNOWLEDGED

    reset = await service.control_service.request_reset(
        session_id="session",
        idempotency_key="reset-wins",
        source_client_type="test",
        source_message_ref={"message_id": 9},
    )

    pause_after = await repositories.controls.get(pause.command.control_id)
    state = await repositories.sessions.get("session")
    assert reset.command.state == ControlState.APPLIED
    assert pause_after.state == ControlState.CANCELLED
    assert state.generation == 1
    assert state.applied_control_sequence == state.pending_control_sequence


@pytest.mark.asyncio
async def test_application_continue_reacquires_same_cycle_runner_without_new_identity(tmp_path):
    service, repositories, active = make_runtime(tmp_path)
    await start(service, active)
    await service.control_service.request_pause(
        session_id="session",
        idempotency_key="pause-api-resume",
        source_client_type="test",
        source_message_ref={"message_id": 10},
    )
    await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.BEFORE_LLM,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    coordinator = SessionExecutionCoordinator()
    resumed_cycle_ids: list[str] = []

    class FakeMCP:
        def can_resume_controlled_cycle(self, *, session_id, cycle_id):
            return session_id == "session" and cycle_id == active.cycle_id

        async def resume_controlled_cycle(self, *, session_id, cycle_id, **kwargs):
            resumed_cycle_ids.append(cycle_id)
            checkpoint = await service.checkpoint_service.run_checkpoint(
                checkpoint=CheckpointName.RESUME,
                active_cycle=active,
                desired_status=CycleStatus.RUNNING,
            )
            assert checkpoint.action == CheckpointAction.CONTINUE
            return AgentResult(
                content="resumed",
                status=AgentStatus.RUNNING,
                session_id=session_id,
                cycle_id=cycle_id,
            )

    fake_api = SimpleNamespace(
        input_admission_service=service,
        input_runtime_repositories=repositories,
        execution_coordinator=coordinator,
        mcp_client=FakeMCP(),
        _resolve_batch_and_capability=lambda *args, **kwargs: None,
        _cycle_status_from_result=lambda result: CycleStatus.RUNNING,
    )

    async def resolve_batch(*args, **kwargs):
        return SimpleNamespace(client_type=ClientType.TELEGRAM), None

    async def assemble(**kwargs):
        raise AssertionError("running resume must not assemble final output")

    fake_api._resolve_batch_and_capability = resolve_batch
    fake_api._assemble_final_if_needed = assemble
    run = await request_runtime_continue(
        fake_api,
        session_id="session",
        idempotency_key="continue-api-resume",
        source_client_type="test",
        source_message_ref={"message_id": 11},
    )

    assert run.agent_result is not None
    assert run.agent_result.cycle_id == "cycle-a"
    assert resumed_cycle_ids == ["cycle-a"]
    state = await repositories.sessions.get("session")
    assert state.active_cycle_id == "cycle-a"
    assert state.cycle_status == CycleStatus.RUNNING
    snapshots = await repositories.snapshots.list_active()
    assert [item.cycle_id for item in snapshots] == ["cycle-a"]
