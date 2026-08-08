from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.ingress.models import ClientResponseRoute
from src.input_runtime import (
    ActiveCycleSnapshot,
    AgentEmission,
    CheckpointName,
    CycleStatus,
    EmissionState,
    FinalizationState,
    SessionInputRuntimeState,
    create_filesystem_input_runtime_repositories,
    new_context_revision_id,
)
from src.input_runtime.errors import InputRuntimeConflictError
from src.input_runtime.finalization import FinalizationBarrierService
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
from src.runtime.finalization_bridge import (
    bind_final_output_assembler,
    bind_output_eligibility,
    clear_finalization_bridge_for_tests,
)
from src.storage import StorageConfigType

NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)
INPUT_BATCH_ID = "ibat_" + "1" * 32


def repositories(tmp_path):
    return create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )


async def seeded_runtime(tmp_path):
    repos = repositories(tmp_path)
    service = FinalizationBarrierService(repositories=repos, clock=lambda: NOW)
    existing = await repos.sessions.get("session")
    if existing is not None:
        return repos, service, existing
    context_id = new_context_revision_id()
    state = SessionInputRuntimeState(
        session_id="session",
        generation=0,
        active_cycle_id="cycle",
        cycle_status=CycleStatus.RUNNING,
        active_context_revision_id=context_id,
        created_at=NOW,
        updated_at=NOW,
    )
    await repos.sessions.create_if_absent(state)
    await repos.snapshots.create_if_absent(
        ActiveCycleSnapshot(
            cycle_id="cycle",
            session_id="session",
            generation=0,
            status=CycleStatus.RUNNING,
            original_input_batch_id=INPUT_BATCH_ID,
            original_user_request="question",
            messages_for_llm=[{"role": "user", "content": "question"}],
            cycle_trace=[],
            applied_input_batch_ids=[INPUT_BATCH_ID],
            applied_through_cycle_sequence=0,
            active_context_revision_id=context_id,
            safe_checkpoint=CheckpointName.BEFORE_LLM,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    return repos, service, state


async def prepared(tmp_path):
    repos, service, _ = await seeded_runtime(tmp_path)
    candidate = await service.capture_candidate(session_id="session", cycle_id="cycle")
    preparation = await service.prepare(candidate)
    assert preparation.prepared and preparation.record is not None
    return repos, service, candidate, preparation.record


async def result_persisted(tmp_path):
    repos, service, candidate, record = await prepared(tmp_path)
    record = await service.persist_result(
        record.finalization_id, {"content": "final", "status": "done"}
    )
    assert record.state == FinalizationState.RESULT_PERSISTED
    return repos, service, candidate, record


async def output_ready(tmp_path, output_batch_id: str | None = None):
    repos, service, candidate, record = await result_persisted(tmp_path)
    output_batch_id = output_batch_id or new_output_batch_id()
    record = await service.mark_output_ready(
        record.finalization_id, output_batch_id=output_batch_id
    )
    assert record.state == FinalizationState.OUTPUT_READY
    return repos, service, candidate, record


async def mutate_state(repos, **updates):
    state = await repos.sessions.get("session")
    assert state is not None
    candidate = state.model_copy(
        update={
            **updates,
            "revision": state.revision + 1,
            "updated_at": NOW + timedelta(seconds=1),
        }
    )
    return await repos.sessions.compare_and_swap(state.revision, candidate)


@pytest.mark.asyncio
async def test_candidate_recheck_aborts_input_accepted_while_final_processing_blocked(tmp_path):
    repos, service, _ = await seeded_runtime(tmp_path)
    candidate = await service.capture_candidate(session_id="session", cycle_id="cycle")
    await mutate_state(repos, active_cycle_accepted_through_sequence=1)
    preparation = await service.prepare(candidate)
    assert preparation.record.state == FinalizationState.ABORTED_NEW_INPUT
    state = await repos.sessions.get("session")
    assert (state.active_cycle_accepted_through_sequence, state.active_cycle_applied_through_sequence) == (1, 0)
    assert state.cycle_status == CycleStatus.RUNNING


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["prepared", "result", "output"])
async def test_second_recheck_aborts_late_input_at_each_durable_phase(tmp_path, phase):
    if phase == "prepared":
        repos, service, _, record = await prepared(tmp_path)
    elif phase == "result":
        repos, service, _, record = await result_persisted(tmp_path)
    else:
        repos, service, _, record = await output_ready(tmp_path)
    await mutate_state(repos, active_cycle_accepted_through_sequence=1)
    if phase == "prepared":
        record = await service.persist_result(record.finalization_id, {"content": "final"})
        record = await service.mark_output_ready(record.finalization_id, output_batch_id=new_output_batch_id())
    elif phase == "result":
        record = await service.mark_output_ready(record.finalization_id, output_batch_id=new_output_batch_id())
    committed = await service.terminal_commit(record.finalization_id)
    assert committed.state == FinalizationState.ABORTED_NEW_INPUT
    state = await repos.sessions.get("session")
    assert state.cycle_status == CycleStatus.RUNNING
    assert state.finalization_id is None
    assert (state.active_cycle_accepted_through_sequence, state.active_cycle_applied_through_sequence) == (1, 0)


@pytest.mark.asyncio
async def test_input_immediately_before_terminal_commit_aborts_stale_output(tmp_path):
    repos, service, _, record = await output_ready(tmp_path)
    await mutate_state(repos, active_cycle_accepted_through_sequence=1)
    committed = await service.terminal_commit(record.finalization_id)
    assert committed.state == FinalizationState.ABORTED_NEW_INPUT
    assert not await repos.finalizations.output_delivery_allowed(
        session_id="session", cycle_id="cycle", output_batch_id=record.output_batch_id
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        ({"pending_control_sequence": 1}, "control_before_terminal"),
        (
            {
                "cycle_status": CycleStatus.PAUSE_REQUESTED,
                "pending_control_sequence": 1,
                "finalization_id": None,
            },
            "control_before_terminal",
        ),
    ],
)
async def test_pending_control_suppresses_finalization(tmp_path, mutation, expected_reason):
    repos, service, _, record = await output_ready(tmp_path)
    await mutate_state(repos, **mutation)
    committed = await service.terminal_commit(record.finalization_id)
    assert committed.state == FinalizationState.ABORTED_CONTROL
    assert committed.cancellation_reason_code == expected_reason
    assert not await repos.finalizations.output_delivery_allowed(
        session_id="session", cycle_id="cycle", output_batch_id=record.output_batch_id
    )


@pytest.mark.asyncio
async def test_reset_generation_fences_old_finalization_without_reverting_new_generation(tmp_path):
    repos, service, _, record = await output_ready(tmp_path)
    await mutate_state(
        repos,
        generation=1,
        active_cycle_id=None,
        cycle_status=CycleStatus.IDLE,
        active_cycle_accepted_through_sequence=0,
        active_cycle_applied_through_sequence=0,
        pending_control_sequence=0,
        applied_control_sequence=0,
        active_context_revision_id=None,
        finalization_id=None,
    )
    committed = await service.terminal_commit(record.finalization_id)
    assert committed.state == FinalizationState.ABORTED_CONTROL
    state = await repos.sessions.get("session")
    assert (state.generation, state.cycle_status, state.active_cycle_id) == (1, CycleStatus.IDLE, None)


@pytest.mark.asyncio
async def test_duplicate_control_without_watermark_change_does_not_phantom_abort(tmp_path):
    repos, service, _ = await seeded_runtime(tmp_path)
    await mutate_state(repos, pending_control_sequence=1, applied_control_sequence=1)
    candidate = await service.capture_candidate(session_id="session", cycle_id="cycle")
    assert candidate.expected_control_sequence == 1
    record = (await service.prepare(candidate)).record
    record = await service.persist_result(record.finalization_id, {"content": "final"})
    record = await service.mark_output_ready(record.finalization_id, output_batch_id=new_output_batch_id())
    committed = await service.terminal_commit(record.finalization_id)
    assert committed.state == FinalizationState.TERMINAL_COMMITTED


@pytest.mark.asyncio
async def test_waiting_commit_rechecks_and_persists_one_question(tmp_path):
    repos, service, state = await seeded_runtime(tmp_path)
    waiting = await service.commit_waiting(
        session_id="session", cycle_id="cycle", generation=0,
        context_revision_id=state.active_context_revision_id,
        expected_input_sequence=0, expected_control_sequence=0,
        waiting_question="Need details?",
    )
    assert waiting.cycle_status == CycleStatus.WAITING_USER
    snapshot = await repos.snapshots.get("cycle")
    assert (snapshot.status, snapshot.waiting_question) == (CycleStatus.WAITING_USER, "Need details?")
    replay = await service.commit_waiting(
        session_id="session", cycle_id="cycle", generation=0,
        context_revision_id=state.active_context_revision_id,
        expected_input_sequence=0, expected_control_sequence=0,
        waiting_question="Need details?",
    )
    assert replay.revision == waiting.revision


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"active_cycle_accepted_through_sequence": 1}, "waiting_aborted_new_input"),
        ({"pending_control_sequence": 1}, "waiting_aborted_control"),
        (
            {
                "generation": 1,
                "active_cycle_id": None,
                "cycle_status": CycleStatus.IDLE,
                "active_context_revision_id": None,
            },
            "waiting_aborted_control",
        ),
    ],
)
async def test_waiting_commit_suppresses_stale_question_on_precommit_races(tmp_path, updates, message):
    repos, service, state = await seeded_runtime(tmp_path)
    await mutate_state(repos, **updates)
    with pytest.raises(InputRuntimeConflictError, match=message):
        await service.commit_waiting(
            session_id="session", cycle_id="cycle", generation=0,
            context_revision_id=state.active_context_revision_id,
            expected_input_sequence=0, expected_control_sequence=0,
            waiting_question="stale?",
        )
    snapshot = await repos.snapshots.get("cycle")
    assert snapshot.status == CycleStatus.RUNNING
    assert snapshot.waiting_question is None


@pytest.mark.asyncio
async def test_post_waiting_input_keeps_same_cycle_authority_for_existing_resume_path(tmp_path):
    repos, service, state = await seeded_runtime(tmp_path)
    await service.commit_waiting(
        session_id="session", cycle_id="cycle", generation=0,
        context_revision_id=state.active_context_revision_id,
        expected_input_sequence=0, expected_control_sequence=0,
        waiting_question="reply?",
    )
    updated = await mutate_state(repos, active_cycle_accepted_through_sequence=1)
    assert (updated.active_cycle_id, updated.cycle_status, updated.active_cycle_accepted_through_sequence) == (
        "cycle", CycleStatus.WAITING_USER, 1
    )


@pytest.mark.asyncio
async def test_finalization_recreation_reuses_stable_identity_and_result(tmp_path):
    _, _, candidate, record = await prepared(tmp_path)
    recreated = repositories(tmp_path)
    service2 = FinalizationBarrierService(repositories=recreated, clock=lambda: NOW + timedelta(seconds=2))
    replay = await service2.prepare(candidate)
    assert replay.record.finalization_id == record.finalization_id
    first = await service2.persist_result(record.finalization_id, {"content": "same"})
    second = await service2.persist_result(record.finalization_id, {"content": "same"})
    assert second.finalization_id == first.finalization_id
    assert second.result_ref == first.result_ref
    assert second.state == FinalizationState.RESULT_PERSISTED


@pytest.mark.asyncio
async def test_result_persisted_record_write_failure_replays_existing_result(tmp_path, monkeypatch):
    _, service, _, record = await prepared(tmp_path)
    import src.input_runtime.ir7_filesystem as adapter
    original = adapter.atomic_write_model
    failed = False

    def injected(path, model):
        nonlocal failed
        if not failed and getattr(model, "state", None) == FinalizationState.RESULT_PERSISTED:
            failed = True
            raise RuntimeError("result state write failed")
        return original(path, model)

    monkeypatch.setattr(adapter, "atomic_write_model", injected)
    with pytest.raises(RuntimeError, match="result state write failed"):
        await service.persist_result(record.finalization_id, {"content": "durable"})
    monkeypatch.setattr(adapter, "atomic_write_model", original)
    recreated = repositories(tmp_path)
    replay = await FinalizationBarrierService(
        repositories=recreated, clock=lambda: NOW + timedelta(seconds=3)
    ).persist_result(record.finalization_id, {"content": "durable"})
    assert replay.state == FinalizationState.RESULT_PERSISTED
    assert replay.result_ref.startswith("finalization-result:")


@pytest.mark.asyncio
async def test_output_ready_state_write_failure_replays_same_output_identity(tmp_path, monkeypatch):
    _, service, _, record = await result_persisted(tmp_path)
    output_id = new_output_batch_id()
    import src.input_runtime.ir7_filesystem as adapter
    original = adapter.atomic_write_model
    failed = False

    def injected(path, model):
        nonlocal failed
        if not failed and getattr(model, "state", None) == FinalizationState.OUTPUT_READY:
            failed = True
            raise RuntimeError("output state write failed")
        return original(path, model)

    monkeypatch.setattr(adapter, "atomic_write_model", injected)
    with pytest.raises(RuntimeError, match="output state write failed"):
        await service.mark_output_ready(record.finalization_id, output_batch_id=output_id)
    monkeypatch.setattr(adapter, "atomic_write_model", original)
    recreated = repositories(tmp_path)
    replay = await FinalizationBarrierService(
        repositories=recreated, clock=lambda: NOW + timedelta(seconds=4)
    ).mark_output_ready(record.finalization_id, output_batch_id=output_id)
    assert (replay.state, replay.output_batch_id) == (FinalizationState.OUTPUT_READY, output_id)


@pytest.mark.asyncio
async def test_partial_terminal_commit_recreation_converges_without_second_commit(tmp_path, monkeypatch):
    repos, service, _, record = await output_ready(tmp_path)
    import src.input_runtime.ir7_filesystem as adapter
    original = adapter.atomic_write_model
    failed = False

    def injected(path, model):
        nonlocal failed
        if not failed and getattr(model, "state", None) == FinalizationState.TERMINAL_COMMITTED:
            failed = True
            raise RuntimeError("terminal marker lost")
        return original(path, model)

    monkeypatch.setattr(adapter, "atomic_write_model", injected)
    with pytest.raises(RuntimeError, match="terminal marker lost"):
        await service.terminal_commit(record.finalization_id)
    partial = await repos.sessions.get("session")
    assert (partial.cycle_status, partial.finalization_id) == (CycleStatus.DONE, record.finalization_id)
    assert not await repos.finalizations.output_delivery_allowed(
        session_id="session", cycle_id="cycle", output_batch_id=record.output_batch_id
    )
    monkeypatch.setattr(adapter, "atomic_write_model", original)
    recreated = repositories(tmp_path)
    service2 = FinalizationBarrierService(repositories=recreated, clock=lambda: NOW + timedelta(seconds=5))
    committed = await service2.terminal_commit(record.finalization_id)
    replay = await service2.terminal_commit(record.finalization_id)
    assert committed.state == FinalizationState.TERMINAL_COMMITTED
    assert replay == committed
    assert await recreated.finalizations.output_delivery_allowed(
        session_id="session", cycle_id="cycle", output_batch_id=record.output_batch_id
    )


def output_batch(output_batch_id: str) -> OutputBatch:
    snapshot = ClientCapabilitySnapshot(
        capability_snapshot_id=new_capability_snapshot_id(),
        capability_contract_version=1,
        client_type="telegram",
        client_instance_id="bot-a",
        features=(), limits={}, fingerprint="sha256:" + "a" * 64, captured_at=NOW,
    )
    return OutputBatch(
        output_batch_id=output_batch_id,
        input_batch_id=INPUT_BATCH_ID,
        session_id="session", cycle_id="cycle", sequence_number=1,
        kind=OutputBatchKind.FINAL,
        response_route=ClientResponseRoute(route_type="telegram", conversation_id="100"),
        locale="en", capability_snapshot=snapshot,
        parts=(TextOutputPart(part_id=new_output_part_id(), index=0, text="final"),),
        state=OutputBatchState.READY, created_at=NOW, ready_at=NOW,
    )


@pytest.mark.asyncio
async def test_real_output_claim_and_outbox_are_fenced_until_terminal_commit(tmp_path):
    clear_finalization_bridge_for_tests()
    _, service, _, record = await result_persisted(tmp_path)
    store = FileSystemOutputBatchStore(tmp_path)
    committed_batch, _ = await store.commit(output_batch(new_output_batch_id()))
    record = await service.mark_output_ready(
        record.finalization_id, output_batch_id=committed_batch.output_batch_id
    )
    bind_output_eligibility(service.output_delivery_allowed)
    bind_final_output_assembler(object())
    try:
        with pytest.raises(OutputBatchConflictError, match="not terminal-committed"):
            await IdempotentOutputClaimService(store).claim(
                committed_batch.output_batch_id,
                claim_request_id=new_output_claim_request_id(), now=NOW,
            )
        outbox = ReadyOutputOutboxService(store)
        assert await outbox.list_ready(
            client_type="telegram", client_instance_id="bot-a",
            minimum_age_seconds=0, now=NOW,
        ) == []
        assert (await service.terminal_commit(record.finalization_id)).state == FinalizationState.TERMINAL_COMMITTED
        ready = await outbox.list_ready(
            client_type="telegram", client_instance_id="bot-a",
            minimum_age_seconds=0, now=NOW,
        )
        assert [item.output_batch_id for item in ready] == [committed_batch.output_batch_id]
        claimed, attempt = await IdempotentOutputClaimService(store).claim(
            committed_batch.output_batch_id,
            claim_request_id=new_output_claim_request_id(), now=NOW,
        )
        assert claimed.state == OutputBatchState.DELIVERING
        assert attempt.startswith("odat_")
    finally:
        clear_finalization_bridge_for_tests()


@pytest.mark.asyncio
async def test_aborted_output_never_becomes_claimable(tmp_path):
    clear_finalization_bridge_for_tests()
    repos, service, _, record = await result_persisted(tmp_path)
    store = FileSystemOutputBatchStore(tmp_path)
    batch, _ = await store.commit(output_batch(new_output_batch_id()))
    record = await service.mark_output_ready(record.finalization_id, output_batch_id=batch.output_batch_id)
    await mutate_state(repos, active_cycle_accepted_through_sequence=1)
    assert (await service.terminal_commit(record.finalization_id)).state == FinalizationState.ABORTED_NEW_INPUT
    bind_output_eligibility(service.output_delivery_allowed)
    bind_final_output_assembler(object())
    try:
        with pytest.raises(OutputBatchConflictError, match="not terminal-committed"):
            await IdempotentOutputClaimService(store).claim(
                batch.output_batch_id,
                claim_request_id=new_output_claim_request_id(), now=NOW,
            )
        assert await ReadyOutputOutboxService(store).list_ready(
            client_type="telegram", client_instance_id="bot-a",
            minimum_age_seconds=0, now=NOW,
        ) == []
    finally:
        clear_finalization_bridge_for_tests()


def emission(context_id: str) -> AgentEmission:
    return AgentEmission(
        session_id="session", cycle_id="cycle", generation=0,
        context_revision_id=context_id, kind="intermediate", text="progress",
        visibility="user", importance="normal",
        response_route={
            "client_type": "telegram", "client_instance_id": "bot-a",
            "conversation_id": "100", "thread_id": None,
        },
        state=EmissionState.READY, idempotency_key="ir7-emission", created_at=NOW,
    )


async def ready_emission(repos, context_id: str):
    accepted = await repos.emissions.accept_intermediate(
        emission(context_id), max_messages=10, min_interval_seconds=0
    )
    assert accepted.accepted
    return accepted.emission


@pytest.mark.asyncio
async def test_emission_claim_linearized_before_terminal_commit_remains_legitimate(tmp_path):
    repos, service, state = await seeded_runtime(tmp_path)
    item = await ready_emission(repos, state.active_context_revision_id)
    _, _, _, record = await output_ready(tmp_path)
    claimed = await repos.emissions.claim_for_client(
        item.emission_id, session_id="session", client_type="telegram",
        client_instance_id="bot-a", claim_token="claim-a",
        claimed_at=NOW, lease_seconds=30,
    )
    assert claimed.state == EmissionState.DELIVERING
    assert (await service.terminal_commit(record.finalization_id)).state == FinalizationState.TERMINAL_COMMITTED
    current = await repos.emissions.get_by_idempotency_key("cycle", "ir7-emission")
    assert (current.state, current.delivery_attempt_count) == (EmissionState.DELIVERING, 1)


@pytest.mark.asyncio
async def test_terminal_commit_linearized_before_emission_claim_prevents_new_attempt(tmp_path):
    repos, service, state = await seeded_runtime(tmp_path)
    item = await ready_emission(repos, state.active_context_revision_id)
    _, _, _, record = await output_ready(tmp_path)
    assert (await service.terminal_commit(record.finalization_id)).state == FinalizationState.TERMINAL_COMMITTED
    with pytest.raises(InputRuntimeConflictError, match="cannot start delivery"):
        await repos.emissions.claim_for_client(
            item.emission_id, session_id="session", client_type="telegram",
            client_instance_id="bot-a", claim_token="claim-b",
            claimed_at=NOW, lease_seconds=30,
        )
    current = await repos.emissions.get_by_idempotency_key("cycle", "ir7-emission")
    assert (current.state, current.delivery_attempt_count) == (EmissionState.CANCELLED, 0)


@pytest.mark.asyncio
async def test_network_side_effect_can_block_after_claim_without_holding_session_authority(tmp_path):
    repos, service, state = await seeded_runtime(tmp_path)
    item = await ready_emission(repos, state.active_context_revision_id)
    _, _, _, record = await output_ready(tmp_path)
    network_started = asyncio.Event()
    network_release = asyncio.Event()

    async def worker():
        claimed = await repos.emissions.claim_for_client(
            item.emission_id, session_id="session", client_type="telegram",
            client_instance_id="bot-a", claim_token="claim-network",
            claimed_at=NOW, lease_seconds=30,
        )
        assert claimed.state == EmissionState.DELIVERING
        network_started.set()
        await network_release.wait()

    task = asyncio.create_task(worker())
    await network_started.wait()
    assert (await service.terminal_commit(record.finalization_id)).state == FinalizationState.TERMINAL_COMMITTED
    network_release.set()
    await task
