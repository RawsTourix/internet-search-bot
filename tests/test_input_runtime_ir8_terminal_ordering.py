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
    InputAdmissionAction,
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

NOW = datetime(2026, 8, 8, 19, 0, tzinfo=timezone.utc)


@dataclass
class Batch:
    input_batch_id: str
    sequence_number: int
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


def repositories(tmp_path):
    return create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )


def service(tmp_path, reader, *, cycle_id_factory=lambda: "cycle-old"):
    repos = repositories(tmp_path)
    coordinator = SessionExecutionCoordinator()
    runtime = InputAdmissionService(
        config=InputRuntimeConfigType(),
        repositories=repos,
        committed_batches=reader,
        wake_coordinator=coordinator,
        cycle_id_factory=cycle_id_factory,
        clock=lambda: NOW,
        payload_size_resolver=lambda batch: batch.payload_size,
    )
    return repos, coordinator, runtime


async def seed_output_ready(tmp_path, reader):
    repos, _, runtime = service(tmp_path, reader)
    outcome = await runtime.admit_committed_batch("initial", session_id="session")
    assert outcome.action == InputAdmissionAction.START_CYCLE
    admission = outcome.admission
    assert admission is not None

    active = ActiveAgentCycle(
        cycle_id=outcome.target_cycle_id,
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
    applier = CycleInputApplier(
        config=runtime.config,
        repositories=repos,
        committed_batches=reader,
        clock=lambda: NOW,
    )
    await applier.ensure_initial_context(
        session_id="session",
        cycle_id=outcome.target_cycle_id,
        generation=0,
        checkpoint=CheckpointName.RESUME,
        active_cycle=active,
        input_batch_id="initial",
    )

    token = "handoff-token"
    assert await runtime.begin_runtime_handoff(admission, handoff_token=token)
    candidate = await runtime.finalization_service.capture_candidate(
        session_id="session",
        cycle_id=outcome.target_cycle_id,
    )
    prepared = await runtime.finalization_service.prepare(candidate)
    assert prepared.record is not None
    record = await runtime.finalization_service.persist_result(
        prepared.record.finalization_id,
        {"content": "stable final result", "status": "done"},
    )
    output_batch_id = new_output_batch_id()
    record = await runtime.finalization_service.mark_output_ready(
        record.finalization_id,
        output_batch_id=output_batch_id,
    )
    assert record.state == FinalizationState.OUTPUT_READY
    return repos, runtime, admission, token, record


async def fresh_recovery(tmp_path, reader):
    fresh, coordinator, runtime = service(
        tmp_path,
        reader,
        cycle_id_factory=lambda: "cycle-new",
    )
    gate = InputRuntimeReadinessGate()
    recovery = InputRuntimeRecoveryCoordinator(
        repositories=fresh,
        admission_service=runtime,
        committed_batches=reader,
        readiness_gate=gate,
        generation_coordinator=coordinator,
        clock=lambda: NOW,
    )
    return fresh, runtime, gate, recovery


@pytest.fixture(autouse=True)
def _fresh_process_handoff_context():
    clear_runtime_handoff_context_for_tests()
    yield
    clear_runtime_handoff_context_for_tests()


@pytest.mark.asyncio
async def test_handed_off_partial_terminal_admits_late_input_before_abort(tmp_path):
    reader = Reader(Batch("initial", 1), Batch("late", 2))
    old, _, admission, token, record = await seed_output_ready(tmp_path, reader)
    marker_before = await old.handoffs.get(admission.admission_id)
    assert marker_before is not None
    assert marker_before.state == RuntimeHandoffState.HANDED_OFF
    clear_runtime_handoff_context_for_tests()

    fresh, _, gate, recovery = await fresh_recovery(tmp_path, reader)
    plan = await recovery.recover()

    late = await fresh.admissions.get_by_input_batch_id("late")
    assert late is not None
    assert late.target_cycle_id == admission.target_cycle_id
    assert late.cycle_sequence == 1
    assert late.state.value == "admitted"

    finalization = await fresh.finalizations.get(record.finalization_id)
    assert finalization is not None
    assert finalization.state == FinalizationState.ABORTED_NEW_INPUT
    assert finalization.finalization_id == record.finalization_id
    assert finalization.result_ref == record.result_ref
    assert finalization.output_batch_id == record.output_batch_id
    assert not await fresh.finalizations.output_delivery_allowed(
        session_id="session",
        cycle_id=admission.target_cycle_id,
        output_batch_id=record.output_batch_id,
    )

    marker = await fresh.handoffs.get(admission.admission_id)
    assert marker is not None
    assert marker.state == RuntimeHandoffState.AMBIGUOUS
    assert marker.handoff_token == token
    state = await fresh.sessions.get("session")
    assert state.active_cycle_id == admission.target_cycle_id
    assert state.cycle_status == CycleStatus.INTERRUPTED
    assert any(
        item.cycle_id == admission.target_cycle_id
        and item.disposition == RecoveryDisposition.AMBIGUOUS
        for item in plan.sessions
    )
    assert gate.is_ready is False


@pytest.mark.asyncio
async def test_completed_partial_terminal_converges_before_late_new_cycle(tmp_path):
    reader = Reader(Batch("initial", 1), Batch("late", 2))
    old, _, admission, token, record = await seed_output_ready(tmp_path, reader)
    completed = await old.handoffs.complete(
        admission.admission_id,
        handoff_token=token,
        completed_at=NOW,
    )
    assert completed.state == RuntimeHandoffState.COMPLETED
    completed_at = completed.completed_at
    # Crash window: invocation completion is durable but terminal projection and
    # TERMINAL_COMMITTED have not yet converged.
    assert (await old.finalizations.get(record.finalization_id)).state == FinalizationState.OUTPUT_READY
    assert (await old.snapshots.get(admission.target_cycle_id)).status == CycleStatus.RUNNING
    clear_runtime_handoff_context_for_tests()

    fresh, _, gate, recovery = await fresh_recovery(tmp_path, reader)
    plan = await recovery.recover()

    committed = await fresh.finalizations.get(record.finalization_id)
    assert committed is not None
    assert committed.state == FinalizationState.TERMINAL_COMMITTED
    assert committed.finalization_id == record.finalization_id
    assert committed.result_ref == record.result_ref
    assert committed.output_batch_id == record.output_batch_id
    marker = await fresh.handoffs.get(admission.admission_id)
    assert marker is not None
    assert marker.state == RuntimeHandoffState.COMPLETED
    assert marker.handoff_token == token
    assert marker.completed_at == completed_at

    old_snapshot = await fresh.snapshots.get(admission.target_cycle_id)
    assert old_snapshot.status == CycleStatus.DONE
    late = await fresh.admissions.get_by_input_batch_id("late")
    assert late is not None
    assert late.admission_kind.value == "start_cycle"
    assert late.target_cycle_id == "cycle-new"
    assert late.target_cycle_id != admission.target_cycle_id
    assert late.cycle_sequence == 0
    state = await fresh.sessions.get("session")
    assert state.active_cycle_id == "cycle-new"
    assert state.cycle_status == CycleStatus.RUNNING
    assert any(
        item.cycle_id == "cycle-new"
        and item.disposition == RecoveryDisposition.START_ADMITTED
        for item in plan.sessions
    )
    assert all(
        not (
            item.cycle_id == admission.target_cycle_id
            and item.should_auto_schedule
        )
        for item in plan.sessions
    )
    assert gate.is_ready is False
