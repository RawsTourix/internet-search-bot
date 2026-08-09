from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.input_runtime import (
    CheckpointAction,
    CheckpointName,
    ControlState,
    CycleStatus,
    FinalizationState,
    InputAdmissionAction,
    InputAdmissionService,
    InputRuntimeConfigType,
    create_filesystem_input_runtime_repositories,
)
from src.input_runtime.diagnostics import InputRuntimeDiagnosticsService
from src.input_runtime.ir9_filesystem import FileSystemRuntimeDiagnosticsReader
from src.runtime import ActiveAgentCycle
from src.storage import StorageConfigType


NOW = datetime(2026, 8, 9, 14, 0, tzinfo=timezone.utc)


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
        self.batches = {item.input_batch_id: item for item in batches}

    async def get_committed(self, input_batch_id: str):
        return self.batches[input_batch_id]


class Wake:
    async def wake(self, session_id: str, *, cycle_id: str) -> bool:
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
                "content": '{"type":"user_request","user_request":"initial"}',
            },
        ],
        cycle_trace=[],
        original_user_message_index=1,
        original_input_batch_id="initial",
        input_runtime_generation=0,
    )


@pytest.mark.asyncio
async def test_ir9_complete_projection_scenario_uses_one_semantic_cycle(tmp_path):
    repositories = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )
    admission = InputAdmissionService(
        config=InputRuntimeConfigType(),
        repositories=repositories,
        committed_batches=Reader(
            Batch("initial", text_parts=[TextPart("i", "message_text", "initial")]),
            Batch("addition", text_parts=[TextPart("a", "message_text", "more")]),
        ),
        wake_coordinator=Wake(),
        cycle_id_factory=lambda: "cycle-a",
        clock=lambda: NOW,
        payload_size_resolver=lambda batch: batch.payload_size,
    )
    diagnostics = InputRuntimeDiagnosticsService(
        FileSystemRuntimeDiagnosticsReader(
            root=repositories.coordination_root,
            locks=repositories.coordination_locks,
        ),
        clock=lambda: NOW,
    )

    initial = await admission.admit_committed_batch(
        "initial",
        session_id="session",
    )
    assert initial.action == InputAdmissionAction.START_CYCLE
    assert initial.admission is not None
    assert initial.should_start_runner is True

    # START_CYCLE admission itself establishes RUNNING durable authority. The
    # production runner then owns a fenced runtime handoff and creates the
    # initial snapshot before ordinary safe checkpoints begin.
    running = await diagnostics.status("session")
    assert running.session_status == CycleStatus.RUNNING
    assert running.initial_request is not None
    assert running.initial_request.cycle_id == "cycle-a"

    handoff_token = "integration-runtime-handoff"
    assert await admission.begin_runtime_handoff(
        initial.admission,
        handoff_token=handoff_token,
    ) is True
    active = active_cycle(initial.target_cycle_id)
    initial_context = await admission.checkpoint_service.ensure_initial_context(
        checkpoint=CheckpointName.RESUME,
        active_cycle=active,
        input_batch_id="initial",
    )
    assert initial_context.action in {
        CheckpointAction.CONTINUE,
        CheckpointAction.INPUT_APPLIED,
    }
    running_after_context = await diagnostics.status("session")
    assert running_after_context.session_status == CycleStatus.RUNNING

    queued_outcome = await admission.admit_committed_batch(
        "addition",
        session_id="session",
    )
    assert queued_outcome.action == InputAdmissionAction.QUEUED_RUNNING
    assert queued_outcome.target_cycle_id == "cycle-a"
    queued = await diagnostics.status("session")
    assert queued.input.queued == 1
    assert queued.additions[0].state.value == "input_addendum_admitted"

    applied_checkpoint = await admission.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.BEFORE_LLM,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert applied_checkpoint.action == CheckpointAction.INPUT_APPLIED
    assert applied_checkpoint.applied_input_batch_ids == ("addition",)
    applied = await diagnostics.status("session")
    assert applied.input.applied_sequence == 1
    assert applied.input.queued == 0
    assert applied.additions[0].state.value == "input_addendum_applied"

    pause = await admission.control_service.request_pause(
        session_id="session",
        idempotency_key="pause",
        source_client_type="test",
        source_message_ref={"message_id": 1},
    )
    assert pause.command.state == ControlState.ACKNOWLEDGED
    pause_requested = await diagnostics.status("session")
    assert pause_requested.session_status == CycleStatus.PAUSE_REQUESTED
    assert pause_requested.controls.effective_state == ControlState.ACKNOWLEDGED

    paused_checkpoint = await admission.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.BEFORE_LLM,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert paused_checkpoint.action == CheckpointAction.PAUSE
    paused = await diagnostics.status("session")
    assert paused.session_status == CycleStatus.PAUSED_BY_USER
    assert paused.paused is True
    assert paused.controls.effective_state == ControlState.APPLIED

    continued = await admission.control_service.request_continue(
        session_id="session",
        idempotency_key="continue",
        source_client_type="test",
        source_message_ref={"message_id": 2},
    )
    assert continued.command.target_cycle_id == "cycle-a"
    resume_checkpoint = await admission.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.RESUME,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert resume_checkpoint.action in {
        CheckpointAction.CONTINUE,
        CheckpointAction.INPUT_APPLIED,
    }
    resumed = await diagnostics.status("session")
    assert resumed.session_status == CycleStatus.RUNNING
    assert resumed.active_cycle_id == "cycle-a"

    candidate = await admission.finalization_service.capture_candidate(
        session_id="session",
        cycle_id="cycle-a",
    )
    prepared = await admission.finalization_service.prepare(candidate)
    assert prepared.record is not None
    record = prepared.record
    record = await admission.finalization_service.persist_result(
        record.finalization_id,
        {"content": "final"},
    )
    record = await admission.finalization_service.mark_output_ready(
        record.finalization_id,
        output_batch_id="obat_" + "9" * 32,
    )
    terminal_record = await admission.finalization_service.terminal_commit(
        record.finalization_id
    )
    assert terminal_record.state == FinalizationState.TERMINAL_COMMITTED
    completed_handoff = await admission.complete_runtime_handoff(
        initial.admission,
        handoff_token=handoff_token,
    )
    assert completed_handoff.state.value == "completed"

    terminal = await diagnostics.status("session")
    assert terminal.session_status == CycleStatus.DONE
    assert terminal.terminal is True
    assert terminal.finalization_state == FinalizationState.TERMINAL_COMMITTED
    assert terminal.handoff_state.value == "completed"
    assert terminal.active_cycle_id == "cycle-a"
    assert terminal.input.applied_sequence == terminal.input.accepted_sequence == 1
