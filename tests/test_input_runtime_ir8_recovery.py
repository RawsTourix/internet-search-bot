from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from src.input_runtime import (
    ActiveCycleSnapshot,
    CheckpointName,
    CycleStatus,
    InputAdmissionService,
    InputRuntimeConfigType,
    create_filesystem_input_runtime_repositories,
    new_context_revision_id,
)
from src.input_runtime.recovery import (
    InputRuntimeLifecycleState,
    InputRuntimeReadinessGate,
    InputRuntimeRecoveryError,
    RecoveryDisposition,
)
from src.input_runtime.recovery_hardening import InputRuntimeRecoveryCoordinator
from src.runtime import SessionExecutionCoordinator
from src.runtime.input_runtime_rehydration import rehydrate_active_agent_cycle
from src.storage import StorageConfigType

NOW = datetime(2026, 8, 8, 18, 0, tzinfo=timezone.utc)


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
        default_factory=lambda: type("Manifest", (), {"items": ()})()
    )

    def model_dump_json(self) -> str:
        return "x" * self.payload_size


class Reader:
    def __init__(self, *batches: Batch) -> None:
        self.batches = {batch.input_batch_id: batch for batch in batches}

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


class NoWake:
    async def wake(self, session_id: str, *, cycle_id: str) -> bool:
        return True


async def make_recovery(tmp_path, *batches: Batch, cycle_ids=None):
    repositories = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )
    reader = Reader(*batches)
    coordinator = SessionExecutionCoordinator()
    ids = iter(cycle_ids or ("cycle-a", "cycle-b", "cycle-c"))
    service = InputAdmissionService(
        config=InputRuntimeConfigType(),
        repositories=repositories,
        committed_batches=reader,
        wake_coordinator=coordinator,
        cycle_id_factory=lambda: next(ids),
        clock=lambda: NOW,
        payload_size_resolver=lambda batch: batch.payload_size,
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
    return repositories, reader, service, coordinator, gate, recovery


def test_readiness_gate_has_explicit_lifecycle_and_rejects_before_ready():
    gate = InputRuntimeReadinessGate()
    assert gate.state == InputRuntimeLifecycleState.STOPPED
    with pytest.raises(InputRuntimeRecoveryError):
        gate.require_ready()
    gate.begin_recovery()
    assert gate.state == InputRuntimeLifecycleState.RECOVERING
    with pytest.raises(InputRuntimeRecoveryError):
        gate.require_ready()
    gate.mark_ready()
    gate.require_ready()
    gate.begin_stopping()
    assert gate.state == InputRuntimeLifecycleState.STOPPING
    gate.mark_stopped()
    assert gate.state == InputRuntimeLifecycleState.STOPPED


def test_recovery_failure_gate_never_becomes_ready():
    gate = InputRuntimeReadinessGate()
    gate.begin_recovery()
    gate.mark_failed("corrupt_history")
    assert gate.state == InputRuntimeLifecycleState.FAILED
    assert gate.failure_reason == "corrupt_history"
    with pytest.raises(InputRuntimeRecoveryError):
        gate.require_ready()


@pytest.mark.asyncio
async def test_committed_without_admission_is_repaired_and_planned_once(tmp_path):
    batch = Batch("initial")
    repositories, reader, _, _, gate, recovery = await make_recovery(
        tmp_path, batch
    )
    plan = await recovery.recover()
    admission = await repositories.admissions.get_by_input_batch_id("initial")
    assert admission is not None
    assert plan.report.committed_unadmitted_admitted == 1
    assert len(plan.sessions) == 1
    assert plan.sessions[0].cycle_id == admission.target_cycle_id
    assert plan.sessions[0].disposition == RecoveryDisposition.START_ADMITTED
    assert gate.state == InputRuntimeLifecycleState.RECOVERING

    # A second fresh process over the same durable root must keep the same
    # admission/cycle identity rather than inventing runner #2 authority.
    fresh_repositories = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )
    fresh_coordinator = SessionExecutionCoordinator()
    fresh_service = InputAdmissionService(
        config=InputRuntimeConfigType(),
        repositories=fresh_repositories,
        committed_batches=reader,
        wake_coordinator=fresh_coordinator,
        cycle_id_factory=lambda: "must-not-be-used",
        clock=lambda: NOW,
        payload_size_resolver=lambda item: item.payload_size,
    )
    fresh_gate = InputRuntimeReadinessGate()
    fresh_recovery = InputRuntimeRecoveryCoordinator(
        repositories=fresh_repositories,
        admission_service=fresh_service,
        committed_batches=reader,
        readiness_gate=fresh_gate,
        generation_coordinator=fresh_coordinator,
        clock=lambda: NOW,
    )
    replay = await fresh_recovery.recover()
    duplicate = await fresh_repositories.admissions.get_by_input_batch_id("initial")
    assert duplicate.admission_id == admission.admission_id
    assert duplicate.target_cycle_id == admission.target_cycle_id
    assert replay.report.committed_unadmitted_admitted == 0


@pytest.mark.asyncio
async def test_multiple_committed_batches_recover_in_authoritative_order(tmp_path):
    batches = (
        Batch("three", sequence_number=3),
        Batch("one", sequence_number=1),
        Batch("two", sequence_number=2),
    )
    repositories, _, _, _, _, recovery = await make_recovery(
        tmp_path, *batches
    )
    await recovery.recover()
    rows = await repositories.admissions.list_for_session("session")
    assert [item.input_batch_id for item in rows] == ["one", "two", "three"]
    assert [item.session_sequence for item in rows] == [1, 2, 3]
    assert [item.cycle_sequence for item in rows] == [0, 1, 2]


@pytest.mark.asyncio
async def test_invalid_committed_store_order_fails_recovery(tmp_path):
    first = Batch("one", sequence_number=2)
    second = Batch("two", sequence_number=2)
    _, _, _, _, gate, recovery = await make_recovery(tmp_path, first, second)
    with pytest.raises(InputRuntimeRecoveryError) as error:
        await recovery.recover()
    assert error.value.reason_code == "committed_batch_order_conflict"
    assert gate.state == InputRuntimeLifecycleState.FAILED


def test_snapshot_rehydration_preserves_exact_runtime_context():
    context_id = new_context_revision_id()
    snapshot = ActiveCycleSnapshot(
        cycle_id="cycle",
        session_id="session",
        generation=7,
        status=CycleStatus.WAITING_USER,
        original_input_batch_id="initial",
        original_user_request="original request",
        messages_for_llm=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": '{"type":"user_request","text":"hello"}'},
            {"role": "assistant", "content": "question"},
        ],
        cycle_trace=[{"type": "checkpoint", "name": "cp"}],
        applied_input_batch_ids=["initial", "addition"],
        applied_through_cycle_sequence=1,
        active_context_revision_id=context_id,
        waiting_question="More data?",
        active_plan_id="plan",
        active_plan_revision=3,
        active_plan_node_id="node",
        artifact_refs=["artifact-a"],
        read_artifact_refs=["artifact-r"],
        result_refs=["result-a"],
        safe_checkpoint=CheckpointName.RESUME,
        snapshot_revision=9,
        created_at=NOW,
        updated_at=NOW,
    )
    cycle = rehydrate_active_agent_cycle(snapshot)
    assert cycle.cycle_id == snapshot.cycle_id
    assert cycle.session_id == snapshot.session_id
    assert cycle.input_runtime_generation == 7
    assert cycle.original_input_batch_id == "initial"
    assert cycle.original_user_request == "original request"
    assert cycle.messages_for_llm == snapshot.messages_for_llm
    assert cycle.cycle_trace == snapshot.cycle_trace
    assert cycle.waiting_question == "More data?"
    assert cycle.active_context_revision_id == context_id
    assert cycle.applied_input_batch_ids == ["initial", "addition"]
    assert cycle.applied_through_cycle_sequence == 1
    assert cycle.input_runtime_safe_checkpoint == CheckpointName.RESUME.value
    assert cycle.input_runtime_snapshot_revision == 9
    assert cycle.artifact_refs == ["artifact-a"]
    assert cycle.read_artifact_refs == ["artifact-r"]
    assert cycle.result_refs == ["result-a"]
    assert cycle.active_plan_id == "plan"
    assert cycle.active_plan_revision == 3
    assert cycle.active_plan_node_id == "node"


def test_snapshot_rehydration_rejects_missing_user_protocol_root():
    snapshot = ActiveCycleSnapshot(
        cycle_id="cycle",
        session_id="session",
        generation=0,
        status=CycleStatus.RUNNING,
        original_input_batch_id="initial",
        original_user_request="request",
        messages_for_llm=[{"role": "system", "content": "system"}],
        cycle_trace=[],
        applied_input_batch_ids=["initial"],
        applied_through_cycle_sequence=0,
        active_context_revision_id=new_context_revision_id(),
        safe_checkpoint=CheckpointName.BEFORE_LLM,
        created_at=NOW,
        updated_at=NOW,
    )
    with pytest.raises(ValueError, match="original user message"):
        rehydrate_active_agent_cycle(snapshot)


def test_recovery_tool_protocol_rejects_incomplete_assistant_block():
    with pytest.raises(InputRuntimeRecoveryError) as error:
        InputRuntimeRecoveryCoordinator._validate_message_protocol(
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "call-1", "function": {"name": "tool"}}
                    ],
                }
            ]
        )
    assert error.value.reason_code == "incomplete_tool_result_block"


@pytest.mark.asyncio
async def test_recovered_reservation_prevents_second_runner_and_shutdown_cancels(tmp_path):
    coordinator = SessionExecutionCoordinator()
    await coordinator.install_recovered_reservation(
        session_id="session",
        cycle_id="cycle",
        generation=4,
    )
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def owner():
        try:
            async with coordinator.admitted_run_lease(
                session_id="session",
                input_batch_id="initial",
                cycle_id="cycle",
                expected_generation=4,
            ) as acquired:
                assert acquired is True
                entered.set()
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    task = asyncio.create_task(owner())
    await entered.wait()
    async with coordinator.admitted_run_lease(
        session_id="session",
        input_batch_id="addition",
        cycle_id="cycle",
        expected_generation=4,
    ) as acquired:
        assert acquired is False
    await coordinator.shutdown()
    await cancelled.wait()
    result = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
