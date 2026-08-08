from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.input_runtime import (
    AgentEmission,
    CheckpointName,
    CycleInputApplier,
    CycleStatus,
    EmissionState,
    FinalizationState,
    InputAdmissionService,
    InputRuntimeConfigType,
    clear_runtime_handoff_context_for_tests,
    create_filesystem_input_runtime_repositories,
)
from src.input_runtime.recovery import InputRuntimeReadinessGate
from src.input_runtime.recovery_hardening import InputRuntimeRecoveryCoordinator
from src.interaction.ids import new_output_batch_id
from src.runtime import ActiveAgentCycle, SessionExecutionCoordinator
from src.storage import StorageConfigType

NOW = datetime(2026, 8, 8, 20, 15, tzinfo=timezone.utc)


@dataclass
class Batch:
    input_batch_id: str = "initial"
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
    def __init__(self, batch: Batch) -> None:
        self.batch = batch

    async def get_committed(self, input_batch_id: str):
        if input_batch_id != self.batch.input_batch_id:
            raise KeyError(input_batch_id)
        return self.batch

    async def list_committed_for_recovery(self):
        return (self.batch,)


def make_runtime(tmp_path, reader):
    repositories = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )
    coordinator = SessionExecutionCoordinator()
    service = InputAdmissionService(
        config=InputRuntimeConfigType(),
        repositories=repositories,
        committed_batches=reader,
        wake_coordinator=coordinator,
        cycle_id_factory=lambda: "cycle",
        clock=lambda: NOW,
        payload_size_resolver=lambda batch: batch.payload_size,
    )
    return repositories, coordinator, service


async def seed_running(tmp_path):
    reader = Reader(Batch())
    repositories, _, service = make_runtime(tmp_path, reader)
    outcome = await service.admit_committed_batch("initial", session_id="session")
    cycle = ActiveAgentCycle(
        cycle_id=outcome.target_cycle_id,
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
    applier = CycleInputApplier(
        config=service.config,
        repositories=repositories,
        committed_batches=reader,
        clock=lambda: NOW,
    )
    await applier.ensure_initial_context(
        session_id="session",
        cycle_id=outcome.target_cycle_id,
        generation=0,
        checkpoint=CheckpointName.RESUME,
        active_cycle=cycle,
        input_batch_id="initial",
    )
    snapshot = await repositories.snapshots.get(outcome.target_cycle_id)
    emission = AgentEmission(
        session_id="session",
        cycle_id=outcome.target_cycle_id,
        generation=0,
        context_revision_id=snapshot.active_context_revision_id,
        kind="intermediate",
        text="durable progress",
        response_route={
            "client_type": "telegram",
            "client_instance_id": "bot",
            "conversation_id": "chat",
        },
        idempotency_key="progress:1",
        created_at=NOW,
    )
    accepted = await repositories.emissions.accept_intermediate(
        emission,
        max_messages=10,
        min_interval_seconds=0,
    )
    assert accepted.accepted is True
    return reader, repositories, service, outcome, accepted.emission


async def recover(tmp_path, reader, *, now):
    repositories, coordinator, service = make_runtime(tmp_path, reader)
    gate = InputRuntimeReadinessGate()
    recovery = InputRuntimeRecoveryCoordinator(
        repositories=repositories,
        admission_service=service,
        committed_batches=reader,
        readiness_gate=gate,
        generation_coordinator=coordinator,
        clock=lambda: now,
    )
    plan = await recovery.recover()
    return repositories, gate, plan


@pytest.fixture(autouse=True)
def _fresh_handoff_context():
    clear_runtime_handoff_context_for_tests()
    yield
    clear_runtime_handoff_context_for_tests()


@pytest.mark.asyncio
async def test_ready_emission_survives_restart_without_transport_send(tmp_path):
    reader, _, _, _, emission = await seed_running(tmp_path)
    fresh, gate, plan = await recover(tmp_path, reader, now=NOW)
    recovered = await fresh.emissions.get(emission.emission_id)
    assert recovered.state == EmissionState.READY
    assert recovered.delivery_attempt_count == 0
    assert plan.report.emissions_retained == 1
    assert gate.is_ready is False


@pytest.mark.asyncio
async def test_expired_delivering_becomes_unknown_never_ready(tmp_path):
    reader, repositories, _, _, emission = await seed_running(tmp_path)
    claimed = await repositories.emissions.claim_delivery(
        emission.emission_id,
        claim_token="claim",
        claimed_at=NOW,
        lease_seconds=1,
    )
    assert claimed.state == EmissionState.DELIVERING

    fresh, _, plan = await recover(
        tmp_path,
        reader,
        now=NOW + timedelta(seconds=2),
    )
    recovered = await fresh.emissions.get(emission.emission_id)
    assert recovered.state == EmissionState.UNKNOWN
    assert recovered.error_code == "delivery_claim_expired"
    assert recovered.delivery_claim_token is None
    assert recovered.delivery_attempt_count == 1
    assert plan.report.emissions_unknown == 1
    assert await fresh.emissions.list_pending_delivery() == ()


@pytest.mark.asyncio
async def test_unknown_emission_survives_restart_and_is_not_rearmed(tmp_path):
    reader, repositories, _, _, emission = await seed_running(tmp_path)
    await repositories.emissions.claim_delivery(
        emission.emission_id,
        claim_token="claim",
        claimed_at=NOW,
        lease_seconds=30,
    )
    unknown = await repositories.emissions.fail_delivery(
        emission.emission_id,
        claim_token="claim",
        state=EmissionState.UNKNOWN.value,
        error_code="transport_outcome_unknown",
    )
    assert unknown.state == EmissionState.UNKNOWN

    fresh, _, _ = await recover(tmp_path, reader, now=NOW + timedelta(seconds=60))
    recovered = await fresh.emissions.get(emission.emission_id)
    assert recovered.state == EmissionState.UNKNOWN
    assert recovered.error_code == "transport_outcome_unknown"
    assert recovered.delivery_attempt_count == 1
    assert await fresh.emissions.list_pending_delivery() == ()


@pytest.mark.asyncio
async def test_terminal_old_cycle_ready_is_cancelled_not_claimed(tmp_path):
    reader, repositories, service, outcome, emission = await seed_running(tmp_path)
    admission = outcome.admission
    assert admission is not None
    assert await service.begin_runtime_handoff(
        admission,
        handoff_token="token",
    )
    candidate = await service.finalization_service.capture_candidate(
        session_id="session",
        cycle_id=outcome.target_cycle_id,
    )
    prepared = await service.finalization_service.prepare(candidate)
    record = await service.finalization_service.persist_result(
        prepared.record.finalization_id,
        {"content": "final", "status": "done"},
    )
    record = await service.finalization_service.mark_output_ready(
        record.finalization_id,
        output_batch_id=new_output_batch_id(),
    )
    assert record.state == FinalizationState.OUTPUT_READY
    await service.complete_runtime_handoff(
        admission,
        handoff_token="token",
    )
    terminal = await service.finalization_service.terminal_commit(
        record.finalization_id
    )
    assert terminal.state == FinalizationState.TERMINAL_COMMITTED
    clear_runtime_handoff_context_for_tests()

    fresh, _, plan = await recover(tmp_path, reader, now=NOW)
    recovered = await fresh.emissions.get(emission.emission_id)
    assert recovered.state == EmissionState.CANCELLED
    assert recovered.cancellation_reason_code == "terminal_cycle_startup_fence"
    assert recovered.delivery_attempt_count == 0
    assert plan.report.emissions_cancelled == 1
    assert await fresh.emissions.list_pending_delivery() == ()
    assert (await fresh.sessions.get("session")).cycle_status == CycleStatus.DONE
