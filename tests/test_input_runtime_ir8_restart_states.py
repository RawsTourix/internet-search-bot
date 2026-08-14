from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.input_runtime import (
    CheckpointName,
    ControlState,
    CycleInputApplier,
    CycleStatus,
    InputAdmissionService,
    InputRuntimeConfigType,
    create_filesystem_input_runtime_repositories,
)
from src.input_runtime.recovery import (
    InputRuntimeReadinessGate,
    RecoveryDisposition,
)
from src.input_runtime.recovery_hardening import InputRuntimeRecoveryCoordinator
from src.runtime import ActiveAgentCycle, SessionExecutionCoordinator
from src.runtime.input_runtime_rehydration import rehydrate_active_agent_cycle
from src.storage import StorageConfigType

NOW = datetime(2026, 8, 8, 18, 30, tzinfo=timezone.utc)


@dataclass
class Batch:
    input_batch_id: str
    session_id: str = "session"
    sequence_number: int = 1
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
    def __init__(self, *batches: Batch):
        self.batches = {item.input_batch_id: item for item in batches}

    async def get_committed(self, input_batch_id: str):
        return self.batches[input_batch_id]

    async def list_committed_for_recovery(self):
        return tuple(
            sorted(
                self.batches.values(),
                key=lambda item: (
                    item.session_id,
                    item.sequence_number,
                    item.committed_at,
                    item.input_batch_id,
                ),
            )
        )


def make_bundle(tmp_path):
    return create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )


def make_service(tmp_path, reader, *, cycle_id_factory=lambda: "cycle-a"):
    repositories = make_bundle(tmp_path)
    coordinator = SessionExecutionCoordinator()
    service = InputAdmissionService(
        config=InputRuntimeConfigType(),
        repositories=repositories,
        committed_batches=reader,
        wake_coordinator=coordinator,
        cycle_id_factory=cycle_id_factory,
        clock=lambda: NOW,
        payload_size_resolver=lambda batch: batch.payload_size,
    )
    return repositories, coordinator, service


def runtime_cycle(cycle_id: str) -> ActiveAgentCycle:
    return ActiveAgentCycle(
        cycle_id=cycle_id,
        session_id="session",
        original_user_request="initial request",
        messages_for_llm=[
            {"role": "system", "content": "system"},
            {
                "role": "user",
                "content": json.dumps(
                    {"type": "user_request", "user_request": "initial request"}
                ),
            },
        ],
        cycle_trace=[],
        original_user_message_index=1,
        original_input_batch_id="initial",
        input_runtime_generation=0,
    )


async def seed_running_cycle(tmp_path):
    batch = Batch("initial")
    reader = Reader(batch)
    repositories, coordinator, service = make_service(tmp_path, reader)
    admitted = await service.admit_committed_batch("initial", session_id="session")
    cycle = runtime_cycle(admitted.target_cycle_id)
    applier = CycleInputApplier(
        config=service.config,
        repositories=repositories,
        committed_batches=reader,
        clock=lambda: NOW,
    )
    await applier.ensure_initial_context(
        session_id="session",
        cycle_id=admitted.target_cycle_id,
        generation=0,
        checkpoint=CheckpointName.RESUME,
        active_cycle=cycle,
        input_batch_id="initial",
    )
    return batch, reader, repositories, coordinator, service, admitted, cycle


async def fresh_recovery(tmp_path, reader):
    repositories, coordinator, service = make_service(
        tmp_path,
        reader,
        cycle_id_factory=lambda: "must-not-create-cycle",
    )
    gate = InputRuntimeReadinessGate()
    recovery = InputRuntimeRecoveryCoordinator(
        repositories=repositories,
        admission_service=service,
        committed_batches=reader,
        readiness_gate=gate,
        generation_coordinator=coordinator,
        clock=lambda: NOW,
    )
    return repositories, coordinator, service, gate, recovery


@pytest.mark.asyncio
async def test_running_pre_handoff_restart_is_safe_same_cycle_plan(tmp_path):
    _, reader, _, _, _, admitted, _ = await seed_running_cycle(tmp_path)
    fresh, _, _, gate, recovery = await fresh_recovery(tmp_path, reader)
    plan = await recovery.recover()
    assert gate.is_ready is False
    assert len(plan.sessions) == 1
    session = plan.sessions[0]
    assert session.cycle_id == admitted.target_cycle_id
    assert session.disposition == RecoveryDisposition.AUTO_RESUME_SAFE
    assert session.snapshot.status == CycleStatus.INTERRUPTED
    assert session.snapshot.interruption_reason == "startup_safe_restart"
    durable = await fresh.sessions.get("session")
    assert durable.active_cycle_id == admitted.target_cycle_id
    assert durable.cycle_status == CycleStatus.INTERRUPTED


@pytest.mark.asyncio
async def test_handed_off_restart_becomes_ambiguous_and_never_auto_schedules(tmp_path):
    _, reader, _, _, service, admitted, _ = await seed_running_cycle(tmp_path)
    token = "handoff-token"
    assert await service.begin_runtime_handoff(
        admitted.admission,
        handoff_token=token,
    ) is True

    fresh, _, _, _, recovery = await fresh_recovery(tmp_path, reader)
    plan = await recovery.recover()
    session = plan.sessions[0]
    assert session.cycle_id == admitted.target_cycle_id
    assert session.disposition == RecoveryDisposition.AMBIGUOUS
    assert session.should_auto_schedule is False
    marker = await fresh.handoffs.get(admitted.admission.admission_id)
    assert marker.state.value == "ambiguous"
    assert marker.handoff_token == token
    durable = await fresh.sessions.get("session")
    assert durable.cycle_status == CycleStatus.INTERRUPTED


@pytest.mark.asyncio
async def test_pause_requested_converges_to_paused_from_existing_safe_snapshot(tmp_path):
    _, reader, _, _, service, admitted, _ = await seed_running_cycle(tmp_path)
    pause = await service.control_service.request_pause(
        session_id="session",
        idempotency_key="pause-1",
        source_client_type="test",
        reason="pause",
    )
    assert pause.command.state == ControlState.ACKNOWLEDGED

    fresh, _, _, _, recovery = await fresh_recovery(tmp_path, reader)
    plan = await recovery.recover()
    session = plan.sessions[0]
    assert session.cycle_id == admitted.target_cycle_id
    assert session.disposition == RecoveryDisposition.PAUSED
    state = await fresh.sessions.get("session")
    snapshot = await fresh.snapshots.get(admitted.target_cycle_id)
    command = await fresh.controls.get(pause.command.control_id)
    assert state.cycle_status == CycleStatus.PAUSED_BY_USER
    assert snapshot.status == CycleStatus.PAUSED_BY_USER
    assert command.state == ControlState.APPLIED
    assert state.applied_control_sequence == state.pending_control_sequence

    rehydrated = rehydrate_active_agent_cycle(snapshot)
    assert rehydrated.cycle_id == admitted.target_cycle_id
    assert rehydrated.active_context_revision_id == snapshot.active_context_revision_id
    assert rehydrated.messages_for_llm == snapshot.messages_for_llm


@pytest.mark.asyncio
async def test_waiting_user_survives_fresh_restart_with_question_and_context(tmp_path):
    _, reader, repositories, _, _, admitted, _ = await seed_running_cycle(tmp_path)
    snapshot = await repositories.snapshots.get(admitted.target_cycle_id)
    waiting_snapshot = snapshot.model_copy(
        update={
            "status": CycleStatus.WAITING_USER,
            "waiting_question": "Which option?",
            "safe_checkpoint": CheckpointName.BEFORE_WAITING,
            "snapshot_revision": snapshot.snapshot_revision + 1,
            "updated_at": NOW,
        }
    )
    await repositories.snapshots.compare_and_swap(
        snapshot.snapshot_revision,
        waiting_snapshot,
    )
    state = await repositories.sessions.get("session")
    waiting_state = state.model_copy(
        update={
            "cycle_status": CycleStatus.WAITING_USER,
            "revision": state.revision + 1,
            "updated_at": NOW,
        }
    )
    await repositories.sessions.compare_and_swap(state.revision, waiting_state)

    fresh, _, _, _, recovery = await fresh_recovery(tmp_path, reader)
    plan = await recovery.recover()
    session = plan.sessions[0]
    assert session.cycle_id == admitted.target_cycle_id
    assert session.disposition == RecoveryDisposition.WAITING
    assert session.snapshot.waiting_question == "Which option?"
    rehydrated = rehydrate_active_agent_cycle(session.snapshot)
    assert rehydrated.cycle_id == admitted.target_cycle_id
    assert rehydrated.waiting_question == "Which option?"
    assert rehydrated.messages_for_llm == session.snapshot.messages_for_llm
    assert (await fresh.sessions.get("session")).cycle_status == CycleStatus.WAITING_USER
