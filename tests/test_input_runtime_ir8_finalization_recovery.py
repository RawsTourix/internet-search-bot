from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.input_runtime import (
    CheckpointName,
    CycleInputApplier,
    CycleStatus,
    FinalizationState,
    InputAdmissionService,
    InputRuntimeConfigType,
    RuntimeHandoffState,
    clear_runtime_handoff_context_for_tests,
    create_filesystem_input_runtime_repositories,
)
from src.input_runtime.recovery import InputRuntimeReadinessGate, RecoveryDisposition
from src.input_runtime.recovery_hardening import InputRuntimeRecoveryCoordinator
from src.interaction.ids import new_output_batch_id
from src.runtime import ActiveAgentCycle, SessionExecutionCoordinator
from src.storage import StorageConfigType

NOW = datetime(2026, 8, 8, 20, 0, tzinfo=timezone.utc)


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


class ExactOutputRecovery:
    def __init__(self, output_batch_id: str) -> None:
        self.output_batch_id = output_batch_id
        self.payloads: list[dict] = []
        self.validated: list[str] = []

    async def validate_output_ready(self, record) -> None:
        self.validated.append(record.finalization_id)

    async def recover_final_output(self, *, record, result_payload: dict) -> str:
        self.payloads.append(dict(result_payload))
        return self.output_batch_id


def make_runtime(tmp_path, reader, *, cycle_factory=lambda: "cycle"):
    repositories = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )
    coordinator = SessionExecutionCoordinator()
    service = InputAdmissionService(
        config=InputRuntimeConfigType(),
        repositories=repositories,
        committed_batches=reader,
        wake_coordinator=coordinator,
        cycle_id_factory=cycle_factory,
        clock=lambda: NOW,
        payload_size_resolver=lambda batch: batch.payload_size,
    )
    return repositories, coordinator, service


async def seed_prepared(tmp_path):
    reader = Reader(Batch())
    repositories, _, service = make_runtime(tmp_path, reader)
    outcome = await service.admit_committed_batch("initial", session_id="session")
    admission = outcome.admission
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
    assert await service.begin_runtime_handoff(
        admission,
        handoff_token="token",
    )
    candidate = await service.finalization_service.capture_candidate(
        session_id="session",
        cycle_id=outcome.target_cycle_id,
    )
    prepared = await service.finalization_service.prepare(candidate)
    assert prepared.record is not None
    return reader, repositories, service, admission, prepared.record


async def fresh_recovery(tmp_path, reader, *, output_recovery=None):
    repositories, coordinator, service = make_runtime(
        tmp_path,
        reader,
        cycle_factory=lambda: "must-not-create",
    )
    gate = InputRuntimeReadinessGate()
    recovery = InputRuntimeRecoveryCoordinator(
        repositories=repositories,
        admission_service=service,
        committed_batches=reader,
        readiness_gate=gate,
        generation_coordinator=coordinator,
        final_output_recovery=output_recovery,
        clock=lambda: NOW,
    )
    return repositories, gate, recovery


@pytest.fixture(autouse=True)
def _fresh_process_context():
    clear_runtime_handoff_context_for_tests()
    yield
    clear_runtime_handoff_context_for_tests()


@pytest.mark.asyncio
async def test_prepared_is_not_replayed_and_becomes_explicit_interruption(tmp_path):
    reader, _, _, admission, prepared = await seed_prepared(tmp_path)
    clear_runtime_handoff_context_for_tests()

    fresh, gate, recovery = await fresh_recovery(tmp_path, reader)
    plan = await recovery.recover()

    record = await fresh.finalizations.get(prepared.finalization_id)
    assert record.state == FinalizationState.ABORTED_CONTROL
    assert record.result_ref is None
    assert record.output_batch_id is None
    marker = await fresh.handoffs.get(admission.admission_id)
    assert marker.state == RuntimeHandoffState.AMBIGUOUS
    state = await fresh.sessions.get("session")
    assert state.cycle_status == CycleStatus.INTERRUPTED
    assert any(
        item.cycle_id == admission.target_cycle_id
        and item.disposition == RecoveryDisposition.AMBIGUOUS
        for item in plan.sessions
    )
    assert gate.is_ready is False


@pytest.mark.asyncio
async def test_result_persisted_reuses_exact_result_then_converges_locally(tmp_path):
    reader, old, service, admission, prepared = await seed_prepared(tmp_path)
    payload = {
        "content": "stable final result",
        "status": "done",
        "session_id": "session",
        "cycle_id": admission.target_cycle_id,
    }
    persisted = await service.finalization_service.persist_result(
        prepared.finalization_id,
        payload,
    )
    assert persisted.state == FinalizationState.RESULT_PERSISTED
    result_ref = persisted.result_ref
    output_batch_id = new_output_batch_id()
    output_recovery = ExactOutputRecovery(output_batch_id)
    clear_runtime_handoff_context_for_tests()

    fresh, _, recovery = await fresh_recovery(
        tmp_path,
        reader,
        output_recovery=output_recovery,
    )
    await recovery.recover()

    record = await fresh.finalizations.get(prepared.finalization_id)
    assert record.state == FinalizationState.TERMINAL_COMMITTED
    assert record.finalization_id == prepared.finalization_id
    assert record.result_ref == result_ref
    assert record.output_batch_id == output_batch_id
    assert output_recovery.payloads == [payload]
    marker = await fresh.handoffs.get(admission.admission_id)
    assert marker.state == RuntimeHandoffState.COMPLETED
    snapshot = await fresh.snapshots.get(admission.target_cycle_id)
    assert snapshot.status == CycleStatus.DONE
    state = await fresh.sessions.get("session")
    assert state.cycle_status == CycleStatus.DONE
    assert state.finalization_id == prepared.finalization_id
    assert await fresh.finalizations.output_delivery_allowed(
        session_id="session",
        cycle_id=admission.target_cycle_id,
        output_batch_id=output_batch_id,
    )
