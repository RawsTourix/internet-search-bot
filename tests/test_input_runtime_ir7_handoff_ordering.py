from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

from src.ingress.models import ClientResponseRoute
from src.input_runtime import (
    ActiveCycleSnapshot,
    CheckpointName,
    CycleFinalizationRecord,
    CycleStatus,
    FinalizationState,
    InputAdmissionService,
    InputRuntimeConfigType,
    RuntimeHandoffRecord,
    RuntimeHandoffState,
    SessionInputRuntimeState,
    clear_input_runtime_binding_for_tests,
    create_filesystem_input_runtime_repositories,
)
from src.input_runtime.finalization import FinalizationBarrierService
from src.input_runtime.handoff_context import (
    clear_runtime_handoff_context_for_tests,
)
from src.interaction.capabilities import ClientCapabilitySnapshot
from src.interaction.errors import OutputBatchConflictError
from src.interaction.ids import (
    new_capability_snapshot_id,
    new_output_batch_id,
    new_output_claim_request_id,
    new_output_part_id,
)
from src.interaction.output_claim import IdempotentOutputClaimService
from src.interaction.output_models import (
    OutputBatch,
    OutputBatchKind,
    OutputBatchState,
    TextOutputPart,
)
from src.interaction.output_outbox import ReadyOutputOutboxService
from src.interaction.output_store import FileSystemOutputBatchStore
from src.runtime import ActiveAgentCycle
from src.runtime.finalization_bridge import bind_final_output_assembler
from src.storage import StorageConfigType


NOW = datetime(2026, 8, 8, 13, 0, tzinfo=timezone.utc)
INPUT_BATCH_ID = "initial"


@pytest.fixture(autouse=True)
def _clear_process_local_runtime_bindings():
    clear_input_runtime_binding_for_tests()
    clear_runtime_handoff_context_for_tests()
    yield
    clear_input_runtime_binding_for_tests()
    clear_runtime_handoff_context_for_tests()


@dataclass
class Batch:
    input_batch_id: str
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
        default_factory=lambda: type("Manifest", (), {"items": ()})()
    )

    def model_dump_json(self) -> str:
        return "x" * self.payload_size


class Reader:
    def __init__(self, *batches: Batch) -> None:
        self.batches = {batch.input_batch_id: batch for batch in batches}

    async def get_committed(self, input_batch_id: str):
        return self.batches[input_batch_id]


class Wake:
    async def wake(self, session_id: str, *, cycle_id: str) -> bool:
        return True


def repositories(tmp_path):
    return create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )


def output_batch(output_batch_id: str) -> OutputBatch:
    snapshot = ClientCapabilitySnapshot(
        capability_snapshot_id=new_capability_snapshot_id(),
        capability_contract_version=1,
        client_type="telegram",
        client_instance_id="bot-a",
        features=(),
        limits={},
        fingerprint="sha256:" + "a" * 64,
        captured_at=NOW,
    )
    return OutputBatch(
        output_batch_id=output_batch_id,
        input_batch_id=INPUT_BATCH_ID,
        session_id="session",
        cycle_id="cycle",
        sequence_number=1,
        kind=OutputBatchKind.FINAL,
        response_route=ClientResponseRoute(
            route_type="telegram",
            conversation_id="100",
        ),
        locale="en",
        capability_snapshot=snapshot,
        parts=(
            TextOutputPart(
                part_id=new_output_part_id(),
                index=0,
                text="final",
            ),
        ),
        state=OutputBatchState.READY,
        created_at=NOW,
        ready_at=NOW,
    )


async def output_ready_runtime(tmp_path):
    repos = repositories(tmp_path)
    runtime = InputAdmissionService(
        config=InputRuntimeConfigType(),
        repositories=repos,
        committed_batches=Reader(Batch(INPUT_BATCH_ID)),
        wake_coordinator=Wake(),
        cycle_id_factory=lambda: "cycle",
        clock=lambda: NOW,
        payload_size_resolver=lambda batch: batch.payload_size,
    )
    outcome = await runtime.admit_committed_batch(
        INPUT_BATCH_ID,
        session_id="session",
    )
    admission = outcome.admission
    assert admission is not None
    assert outcome.target_cycle_id == "cycle"

    active = ActiveAgentCycle(
        cycle_id="cycle",
        session_id="session",
        original_user_request="initial",
        messages_for_llm=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": '{"type":"user_request"}'},
        ],
        cycle_trace=[],
        original_user_message_index=1,
        original_input_batch_id=INPUT_BATCH_ID,
        input_runtime_generation=0,
    )
    await runtime.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.RESUME,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )

    handoff_token = "handoff-token"
    assert await runtime.begin_runtime_handoff(
        admission,
        handoff_token=handoff_token,
    )
    candidate = await runtime.finalization_service.capture_candidate(
        session_id="session",
        cycle_id="cycle",
    )
    assert candidate.runtime_handoff_admission_id == admission.admission_id
    assert candidate.runtime_handoff_token == handoff_token
    record = (await runtime.finalization_service.prepare(candidate)).record
    assert record is not None and record.state == FinalizationState.PREPARED
    record = await runtime.finalization_service.persist_result(
        record.finalization_id,
        {"content": "final", "status": "done"},
    )
    assert record.state == FinalizationState.RESULT_PERSISTED

    store = FileSystemOutputBatchStore(tmp_path)
    batch, _ = await store.commit(output_batch(new_output_batch_id()))
    record = await runtime.finalization_service.mark_output_ready(
        record.finalization_id,
        output_batch_id=batch.output_batch_id,
    )
    assert record.state == FinalizationState.OUTPUT_READY
    bind_final_output_assembler(object())
    return runtime, repos, admission, handoff_token, record, store, batch


async def mutate_state(repos, **updates):
    state = await repos.sessions.get("session")
    assert state is not None
    changed = state.model_copy(
        update={
            **updates,
            "revision": state.revision + 1,
            "updated_at": NOW + timedelta(seconds=1),
        }
    )
    return await repos.sessions.compare_and_swap(state.revision, changed)


@pytest.mark.asyncio
async def test_handoff_completion_fault_blocks_every_terminal_authority(tmp_path, monkeypatch):
    runtime, repos, admission, token, record, store, batch = (
        await output_ready_runtime(tmp_path)
    )
    import src.input_runtime.ir7_handoff_ordering as ordering

    original = ordering.atomic_write_model

    def injected(path, model):
        if (
            isinstance(model, RuntimeHandoffRecord)
            and model.state == RuntimeHandoffState.COMPLETED
        ):
            raise RuntimeError("handoff completion write failed")
        return original(path, model)

    monkeypatch.setattr(ordering, "atomic_write_model", injected)
    with pytest.raises(RuntimeError, match="handoff completion write failed"):
        await runtime.finalization_service.terminal_commit(record.finalization_id)

    marker = await repos.handoffs.get(admission.admission_id)
    current = await repos.finalizations.get(record.finalization_id)
    session = await repos.sessions.get("session")
    snapshot = await repos.snapshots.get("cycle")
    assert marker is not None and marker.state == RuntimeHandoffState.HANDED_OFF
    assert current is not None and current.state == FinalizationState.OUTPUT_READY
    assert session is not None and session.cycle_status != CycleStatus.DONE
    assert snapshot is not None and snapshot.status == CycleStatus.RUNNING
    assert not await runtime.finalization_service.output_delivery_allowed(batch)
    assert await ReadyOutputOutboxService(store).list_ready(
        client_type="telegram",
        client_instance_id="bot-a",
        minimum_age_seconds=0,
        now=NOW,
    ) == []
    with pytest.raises(OutputBatchConflictError):
        await IdempotentOutputClaimService(store).claim(
            batch.output_batch_id,
            claim_request_id=new_output_claim_request_id(),
            now=NOW,
        )


@pytest.mark.asyncio
async def test_terminal_writes_persist_handoff_before_snapshot_session_and_marker(tmp_path, monkeypatch):
    runtime, repos, admission, token, record, _, _ = await output_ready_runtime(tmp_path)
    import src.input_runtime.ir7_filesystem as finalization_writes
    import src.input_runtime.ir7_handoff_ordering as ordering

    events: list[str] = []
    original_handoff_write = ordering.atomic_write_model
    original_final_write = finalization_writes.atomic_write_model

    def handoff_write(path, model):
        if (
            isinstance(model, RuntimeHandoffRecord)
            and model.state == RuntimeHandoffState.COMPLETED
        ):
            events.append("handoff_completed")
        return original_handoff_write(path, model)

    def final_write(path, model):
        if isinstance(model, ActiveCycleSnapshot) and model.status == CycleStatus.DONE:
            events.append("terminal_snapshot")
        elif isinstance(model, SessionInputRuntimeState) and model.cycle_status == CycleStatus.DONE:
            events.append("terminal_session")
        elif (
            isinstance(model, CycleFinalizationRecord)
            and model.state == FinalizationState.TERMINAL_COMMITTED
        ):
            events.append("terminal_committed")
        return original_final_write(path, model)

    monkeypatch.setattr(ordering, "atomic_write_model", handoff_write)
    monkeypatch.setattr(finalization_writes, "atomic_write_model", final_write)
    committed = await runtime.finalization_service.terminal_commit(
        record.finalization_id
    )
    assert committed.state == FinalizationState.TERMINAL_COMMITTED
    assert events == [
        "handoff_completed",
        "terminal_snapshot",
        "terminal_session",
        "terminal_committed",
    ]
    marker = await repos.handoffs.get(admission.admission_id)
    assert marker is not None and marker.state == RuntimeHandoffState.COMPLETED
    completed_at = marker.completed_at

    replay = await runtime.complete_runtime_handoff(
        admission,
        handoff_token=token,
    )
    assert replay.state == RuntimeHandoffState.COMPLETED
    assert replay.completed_at == completed_at
    marker_after = await repos.handoffs.get(admission.admission_id)
    assert marker_after == replay


@pytest.mark.asyncio
async def test_output_worker_cannot_claim_before_handoff_and_terminal_marker(tmp_path):
    runtime, repos, admission, _, record, store, batch = await output_ready_runtime(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    original = runtime.finalization_service.repository.commit_terminal_authority

    async def blocked(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    runtime.finalization_service.repository.commit_terminal_authority = blocked
    task = asyncio.create_task(
        runtime.finalization_service.terminal_commit(record.finalization_id)
    )
    await entered.wait()

    marker = await repos.handoffs.get(admission.admission_id)
    assert marker is not None and marker.state == RuntimeHandoffState.HANDED_OFF
    assert await ReadyOutputOutboxService(store).list_ready(
        client_type="telegram",
        client_instance_id="bot-a",
        minimum_age_seconds=0,
        now=NOW,
    ) == []
    with pytest.raises(OutputBatchConflictError):
        await IdempotentOutputClaimService(store).claim(
            batch.output_batch_id,
            claim_request_id=new_output_claim_request_id(),
            now=NOW,
        )

    release.set()
    committed = await task
    assert committed.state == FinalizationState.TERMINAL_COMMITTED
    marker = await repos.handoffs.get(admission.admission_id)
    assert marker is not None and marker.state == RuntimeHandoffState.COMPLETED
    ready = await ReadyOutputOutboxService(store).list_ready(
        client_type="telegram",
        client_instance_id="bot-a",
        minimum_age_seconds=0,
        now=NOW,
    )
    assert [item.output_batch_id for item in ready] == [batch.output_batch_id]
    claimed, _ = await IdempotentOutputClaimService(store).claim(
        batch.output_batch_id,
        claim_request_id=new_output_claim_request_id(),
        now=NOW,
    )
    assert claimed.state == OutputBatchState.DELIVERING


@pytest.mark.asyncio
async def test_completed_handoff_incomplete_terminal_direct_retry_reuses_all_ids(tmp_path, monkeypatch):
    runtime, repos, admission, token, record, _, batch = await output_ready_runtime(tmp_path)
    import src.input_runtime.ir7_filesystem as finalization_writes

    original = finalization_writes.atomic_write_model
    failed = False

    def injected(path, model):
        nonlocal failed
        if (
            not failed
            and isinstance(model, ActiveCycleSnapshot)
            and model.status == CycleStatus.DONE
        ):
            failed = True
            raise RuntimeError("terminal snapshot write failed")
        return original(path, model)

    monkeypatch.setattr(finalization_writes, "atomic_write_model", injected)
    with pytest.raises(RuntimeError, match="terminal snapshot write failed"):
        await runtime.finalization_service.terminal_commit(record.finalization_id)

    marker = await repos.handoffs.get(admission.admission_id)
    current = await repos.finalizations.get(record.finalization_id)
    assert marker is not None and marker.state == RuntimeHandoffState.COMPLETED
    assert marker.handoff_token == token
    completed_at = marker.completed_at
    assert current is not None and current.state == FinalizationState.OUTPUT_READY
    assert current.finalization_id == record.finalization_id
    assert current.result_ref == record.result_ref
    assert current.output_batch_id == batch.output_batch_id
    assert not await runtime.finalization_service.output_delivery_allowed(batch)

    monkeypatch.setattr(finalization_writes, "atomic_write_model", original)
    recreated = repositories(tmp_path)
    retry = FinalizationBarrierService(
        repositories=recreated,
        clock=lambda: NOW + timedelta(seconds=5),
    )
    committed = await retry.terminal_commit(record.finalization_id)
    assert committed.state == FinalizationState.TERMINAL_COMMITTED
    assert committed.finalization_id == record.finalization_id
    assert committed.result_ref == record.result_ref
    assert committed.output_batch_id == batch.output_batch_id
    marker_after = await recreated.handoffs.get(admission.admission_id)
    assert marker_after is not None
    assert marker_after.state == RuntimeHandoffState.COMPLETED
    assert marker_after.handoff_token == token
    assert marker_after.completed_at == completed_at
    assert await retry.output_delivery_allowed(batch)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("updates", "expected_state"),
    [
        (
            {"active_cycle_accepted_through_sequence": 1},
            FinalizationState.ABORTED_NEW_INPUT,
        ),
        (
            {"pending_control_sequence": 1},
            FinalizationState.ABORTED_CONTROL,
        ),
    ],
)
async def test_late_authority_aborts_before_handoff_completion(
    tmp_path,
    updates,
    expected_state,
):
    runtime, repos, admission, _, record, _, batch = await output_ready_runtime(tmp_path)
    await mutate_state(repos, **updates)
    aborted = await runtime.finalization_service.terminal_commit(record.finalization_id)
    assert aborted.state == expected_state
    marker = await repos.handoffs.get(admission.admission_id)
    session = await repos.sessions.get("session")
    assert marker is not None and marker.state == RuntimeHandoffState.HANDED_OFF
    assert session is not None and session.cycle_status == CycleStatus.RUNNING
    assert session.active_cycle_id == "cycle"
    assert not await runtime.finalization_service.output_delivery_allowed(batch)


@pytest.mark.asyncio
async def test_cancellation_before_handoff_completion_keeps_no_terminal_authority(tmp_path, monkeypatch):
    runtime, repos, admission, token, record, _, batch = await output_ready_runtime(tmp_path)
    import src.input_runtime.ir7_handoff_ordering as ordering

    original = ordering.atomic_write_model

    def injected(path, model):
        if (
            isinstance(model, RuntimeHandoffRecord)
            and model.state == RuntimeHandoffState.COMPLETED
        ):
            raise asyncio.CancelledError()
        return original(path, model)

    monkeypatch.setattr(ordering, "atomic_write_model", injected)
    with pytest.raises(asyncio.CancelledError):
        await runtime.finalization_service.terminal_commit(record.finalization_id)
    marker = await repos.handoffs.get(admission.admission_id)
    current = await repos.finalizations.get(record.finalization_id)
    assert marker is not None and marker.state == RuntimeHandoffState.HANDED_OFF
    assert current is not None and current.state == FinalizationState.OUTPUT_READY
    assert not await runtime.finalization_service.output_delivery_allowed(batch)

    ambiguous = await runtime.mark_runtime_handoff_ambiguous(
        admission,
        handoff_token=token,
        error_code="terminal_cancelled_before_handoff_completion",
    )
    assert ambiguous is not None
    assert ambiguous.state == RuntimeHandoffState.AMBIGUOUS


@pytest.mark.asyncio
async def test_cancellation_after_completed_handoff_preserves_completion_and_direct_retry(tmp_path, monkeypatch):
    runtime, repos, admission, token, record, _, batch = await output_ready_runtime(tmp_path)
    import src.input_runtime.ir7_filesystem as finalization_writes

    original = finalization_writes.atomic_write_model

    def injected(path, model):
        if isinstance(model, ActiveCycleSnapshot) and model.status == CycleStatus.DONE:
            raise asyncio.CancelledError()
        return original(path, model)

    monkeypatch.setattr(finalization_writes, "atomic_write_model", injected)
    with pytest.raises(asyncio.CancelledError):
        await runtime.finalization_service.terminal_commit(record.finalization_id)

    marker = await repos.handoffs.get(admission.admission_id)
    assert marker is not None and marker.state == RuntimeHandoffState.COMPLETED
    completed_at = marker.completed_at
    assert not await runtime.finalization_service.output_delivery_allowed(batch)

    cleanup_marker = await runtime.mark_runtime_handoff_ambiguous(
        admission,
        handoff_token=token,
        error_code="terminal_cancelled_after_handoff_completion",
    )
    assert cleanup_marker is not None
    assert cleanup_marker.state == RuntimeHandoffState.COMPLETED
    assert cleanup_marker.completed_at == completed_at

    monkeypatch.setattr(finalization_writes, "atomic_write_model", original)
    recreated = repositories(tmp_path)
    retry = FinalizationBarrierService(
        repositories=recreated,
        clock=lambda: NOW + timedelta(seconds=6),
    )
    committed = await retry.terminal_commit(record.finalization_id)
    assert committed.state == FinalizationState.TERMINAL_COMMITTED
    marker_after = await recreated.handoffs.get(admission.admission_id)
    assert marker_after is not None
    assert marker_after.state == RuntimeHandoffState.COMPLETED
    assert marker_after.completed_at == completed_at
    assert marker_after.handoff_token == token
    assert await retry.output_delivery_allowed(batch)
