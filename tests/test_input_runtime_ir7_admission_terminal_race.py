from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.api.api import Api
from src.ingress.models import ClientResponseRoute
from src.input_runtime import (
    ActiveCycleSnapshot,
    AdmissionKind,
    CheckpointName,
    CycleStatus,
    FinalizationState,
    InputAdmissionAction,
    InputAdmissionDecisionStaleError,
    InputAdmissionRecord,
    InputAdmissionService,
    InputRuntimeConfigType,
    RuntimeHandoffState,
    clear_input_runtime_binding_for_tests,
    create_filesystem_input_runtime_repositories,
)
from src.input_runtime.handoff_context import clear_runtime_handoff_context_for_tests
from src.input_runtime.ir7_admission_ordering import STALE_TERMINAL_ADMISSION_REASON
from src.interaction.capabilities import ClientCapabilitySnapshot
from src.interaction.ids import (
    new_capability_snapshot_id,
    new_output_batch_id,
    new_output_part_id,
)
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

NOW = datetime(2026, 8, 8, 16, 0, tzinfo=timezone.utc)
INITIAL_BATCH_ID = "ibat_" + "1" * 32
LATE_BATCH_ID = "ibat_" + "2" * 32
CYCLE_A = "cycle-a"
CYCLE_B = "cycle-b"


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


def final_output(output_batch_id: str) -> OutputBatch:
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
        input_batch_id=INITIAL_BATCH_ID,
        session_id="session",
        cycle_id=CYCLE_A,
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
    reader = Reader(Batch(INITIAL_BATCH_ID), Batch(LATE_BATCH_ID))
    runtime = InputAdmissionService(
        config=InputRuntimeConfigType(),
        repositories=repos,
        committed_batches=reader,
        wake_coordinator=Wake(),
        cycle_id_factory=lambda: CYCLE_A,
        clock=lambda: NOW,
        payload_size_resolver=lambda batch: batch.payload_size,
    )
    initial = await runtime.admit_committed_batch(
        INITIAL_BATCH_ID,
        session_id="session",
    )
    admission = initial.admission
    assert admission is not None
    assert initial.target_cycle_id == CYCLE_A

    active = ActiveAgentCycle(
        cycle_id=CYCLE_A,
        session_id="session",
        original_user_request="initial",
        messages_for_llm=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": '{"type":"user_request"}'},
        ],
        cycle_trace=[],
        original_user_message_index=1,
        original_input_batch_id=INITIAL_BATCH_ID,
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
        cycle_id=CYCLE_A,
    )
    record = (await runtime.finalization_service.prepare(candidate)).record
    assert record is not None
    assert record.state == FinalizationState.PREPARED
    record = await runtime.finalization_service.persist_result(
        record.finalization_id,
        {"content": "final", "status": "done"},
    )
    assert record.state == FinalizationState.RESULT_PERSISTED

    store = FileSystemOutputBatchStore(tmp_path)
    batch, _ = await store.commit(final_output(new_output_batch_id()))
    record = await runtime.finalization_service.mark_output_ready(
        record.finalization_id,
        output_batch_id=batch.output_batch_id,
    )
    assert record.state == FinalizationState.OUTPUT_READY
    state = await repos.sessions.get("session")
    assert state is not None and state.cycle_status == CycleStatus.FINALIZING

    bind_final_output_assembler(object())
    runtime.cycle_id_factory = lambda: CYCLE_B
    return runtime, repos, admission, record, store, batch


def api_for(runtime: InputAdmissionService) -> Api:
    api = Api.__new__(Api)
    api.input_runtime_config = InputRuntimeConfigType()
    api.input_admission_service = runtime
    return api


@pytest.mark.asyncio
async def test_terminal_wins_reclassifies_stale_admission_to_new_cycle(tmp_path):
    runtime, repos, initial_admission, record, store, batch = (
        await output_ready_runtime(tmp_path)
    )
    api = api_for(runtime)

    stale_candidate_ready = asyncio.Event()
    release_stale_candidate = asyncio.Event()
    original_allocate = repos.admissions.allocate

    async def blocked_allocate(candidate):
        if (
            candidate.input_batch_id == LATE_BATCH_ID
            and candidate.admission_kind == AdmissionKind.CONTINUE_RUNNING
        ):
            assert candidate.target_cycle_id == CYCLE_A
            stale_candidate_ready.set()
            await release_stale_candidate.wait()
        return await original_allocate(candidate)

    repos.admissions.allocate = blocked_allocate
    admission_task = asyncio.create_task(
        api.admit_committed_batch(LATE_BATCH_ID, session_id="session")
    )
    await stale_candidate_ready.wait()

    committed = await runtime.finalization_service.terminal_commit(
        record.finalization_id
    )
    assert committed.state == FinalizationState.TERMINAL_COMMITTED
    marker = await repos.handoffs.get(initial_admission.admission_id)
    assert marker is not None
    assert marker.state == RuntimeHandoffState.COMPLETED

    release_stale_candidate.set()
    try:
        outcome = await admission_task
    except ValidationError as error:  # pragma: no cover - explicit regression fence
        pytest.fail(f"raw ValidationError leaked from stale admission race: {error}")

    assert outcome.action == InputAdmissionAction.START_CYCLE
    assert outcome.admission_kind == AdmissionKind.START_CYCLE
    assert outcome.target_cycle_id == CYCLE_B
    assert outcome.cycle_sequence == 0
    assert outcome.should_start_runner is True
    assert outcome.should_wake_runner is False

    session = await repos.sessions.get("session")
    assert session is not None
    assert session.active_cycle_id == CYCLE_B
    assert session.cycle_status == CycleStatus.RUNNING

    old_finalization = await repos.finalizations.get(record.finalization_id)
    assert old_finalization is not None
    assert old_finalization.state == FinalizationState.TERMINAL_COMMITTED
    assert await runtime.finalization_service.output_delivery_allowed(batch)
    ready = await ReadyOutputOutboxService(store).list_ready(
        client_type="telegram",
        client_instance_id="bot-a",
        minimum_age_seconds=0,
        now=NOW,
    )
    assert [item.output_batch_id for item in ready] == [batch.output_batch_id]

    admissions = await repos.admissions.list_for_session("session")
    late_admissions = [
        item for item in admissions if item.input_batch_id == LATE_BATCH_ID
    ]
    assert len(late_admissions) == 1
    assert late_admissions[0].admission_id == outcome.admission_id
    assert late_admissions[0].target_cycle_id == CYCLE_B
    old_inbox = await repos.inbox.list_for_cycle(CYCLE_A)
    assert all(item.input_batch_id != LATE_BATCH_ID for item in old_inbox)

    duplicate = await api.admit_committed_batch(
        LATE_BATCH_ID,
        session_id="session",
    )
    assert duplicate.action == InputAdmissionAction.DUPLICATE
    assert duplicate.admission_id == outcome.admission_id
    assert duplicate.target_cycle_id == CYCLE_B


@pytest.mark.asyncio
async def test_admission_wins_before_terminal_and_aborts_stale_finalization(tmp_path):
    runtime, repos, initial_admission, record, store, batch = (
        await output_ready_runtime(tmp_path)
    )

    terminal_ready = asyncio.Event()
    release_terminal = asyncio.Event()
    original_commit = (
        runtime.finalization_service.repository.commit_terminal_authority
    )

    async def blocked_terminal(*args, **kwargs):
        terminal_ready.set()
        await release_terminal.wait()
        return await original_commit(*args, **kwargs)

    runtime.finalization_service.repository.commit_terminal_authority = (
        blocked_terminal
    )
    terminal_task = asyncio.create_task(
        runtime.finalization_service.terminal_commit(record.finalization_id)
    )
    await terminal_ready.wait()

    outcome = await runtime.admit_committed_batch(
        LATE_BATCH_ID,
        session_id="session",
    )
    assert outcome.action == InputAdmissionAction.QUEUED_RUNNING
    assert outcome.admission_kind == AdmissionKind.CONTINUE_RUNNING
    assert outcome.target_cycle_id == CYCLE_A
    assert outcome.cycle_sequence == 1
    assert outcome.should_start_runner is False
    assert outcome.should_wake_runner is True

    durable_before_terminal = await repos.sessions.get("session")
    assert durable_before_terminal is not None
    assert durable_before_terminal.active_cycle_id == CYCLE_A
    assert durable_before_terminal.active_cycle_accepted_through_sequence == 1
    assert durable_before_terminal.active_cycle_applied_through_sequence == 0

    release_terminal.set()
    aborted = await terminal_task
    assert aborted.state == FinalizationState.ABORTED_NEW_INPUT

    marker = await repos.handoffs.get(initial_admission.admission_id)
    assert marker is not None
    assert marker.state == RuntimeHandoffState.HANDED_OFF
    session = await repos.sessions.get("session")
    assert session is not None
    assert session.active_cycle_id == CYCLE_A
    assert session.cycle_status == CycleStatus.RUNNING
    assert session.active_cycle_accepted_through_sequence == 1
    assert session.active_cycle_applied_through_sequence == 0
    assert not await runtime.finalization_service.output_delivery_allowed(batch)
    assert await ReadyOutputOutboxService(store).list_ready(
        client_type="telegram",
        client_instance_id="bot-a",
        minimum_age_seconds=0,
        now=NOW,
    ) == []


@pytest.mark.asyncio
async def test_repository_reports_stale_decision_before_any_late_admission_write(tmp_path):
    runtime, repos, _, record, _, _ = await output_ready_runtime(tmp_path)
    committed = await runtime.finalization_service.terminal_commit(
        record.finalization_id
    )
    assert committed.state == FinalizationState.TERMINAL_COMMITTED

    state_before = await repos.sessions.get("session")
    assert state_before is not None
    assert state_before.cycle_status == CycleStatus.DONE

    stale = InputAdmissionRecord(
        session_id="session",
        input_batch_id=LATE_BATCH_ID,
        session_sequence=1,
        target_cycle_id=CYCLE_A,
        cycle_sequence=1,
        admitted_generation=state_before.generation,
        payload_size_bytes=10,
        admission_kind=AdmissionKind.CONTINUE_RUNNING,
        idempotency_key=f"committed-input:{LATE_BATCH_ID}",
        admitted_at=NOW,
    )

    try:
        with pytest.raises(InputAdmissionDecisionStaleError) as captured:
            await repos.admissions.allocate(stale)
    except ValidationError as error:  # pragma: no cover - regression fence
        pytest.fail(f"raw ValidationError leaked from repository: {error}")

    assert captured.value.reason_code == STALE_TERMINAL_ADMISSION_REASON
    assert await repos.admissions.get_by_input_batch_id(LATE_BATCH_ID) is None
    assert await repos.inbox.list_for_cycle(CYCLE_A) == ()
    state_after = await repos.sessions.get("session")
    assert state_after == state_before
