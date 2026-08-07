import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.input_runtime.config import InputRuntimeConfigType
from src.input_runtime.emissions import AgentEmissionService, ManagerToolExecutionContext
from src.input_runtime.errors import InputRuntimeConflictError
from src.input_runtime.factory import create_filesystem_input_runtime_repositories
from src.input_runtime.models import (
    AgentEmission,
    CycleStatus,
    EmissionState,
    SessionInputRuntimeState,
    new_context_revision_id,
)
from src.storage import StorageConfigType


NOW = datetime(2026, 8, 8, 16, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def repos(tmp_path):
    return create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )


def active(tmp_path):
    repository = repos(tmp_path)
    revision = new_context_revision_id()
    state = SessionInputRuntimeState(
        session_id="session",
        generation=0,
        active_cycle_id="cycle",
        cycle_status=CycleStatus.RUNNING,
        active_context_revision_id=revision,
        created_at=NOW,
        updated_at=NOW,
    )
    run(repository.sessions.create_if_absent(state))
    return repository, state


def candidate(state, *, key="stable-key"):
    return AgentEmission(
        session_id=state.session_id,
        cycle_id=state.active_cycle_id,
        generation=state.generation,
        context_revision_id=state.active_context_revision_id,
        kind="intermediate",
        text="durable semantic intent",
        visibility="user",
        importance="normal",
        response_route={
            "client_type": "telegram",
            "client_instance_id": "bot-a",
            "conversation_id": "100",
            "thread_id": "7",
            "capability_snapshot_id": "caps",
        },
        state=EmissionState.READY,
        idempotency_key=key,
        created_at=NOW,
    )


def test_record_durable_then_index_publication_failure_recovers_same_emission(tmp_path, monkeypatch):
    repository, state = active(tmp_path)
    emission = candidate(state)
    original_index = repository.emissions._index
    calls = 0

    def fail_first_index(record):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated index publication crash")
        return original_index(record)

    monkeypatch.setattr(repository.emissions, "_index", fail_first_index)
    with pytest.raises(RuntimeError, match="index publication crash"):
        run(repository.emissions.accept_intermediate(
            emission,
            max_messages=10,
            min_interval_seconds=0,
        ))

    recreated = repos(tmp_path)
    retry = run(recreated.emissions.accept_intermediate(
        emission.model_copy(update={"emission_id": "emit_" + "f" * 32}),
        max_messages=10,
        min_interval_seconds=0,
    ))
    assert retry.accepted is True
    assert retry.duplicate is True
    assert retry.emission.emission_id == emission.emission_id
    assert len(run(recreated.emissions.list_pending_delivery())) == 1


def test_cancelled_manager_task_after_ready_persistence_replays_same_emission(tmp_path):
    repository, state = active(tmp_path)
    batch = SimpleNamespace(
        input_batch_id="batch",
        session_id="session",
        response_route=SimpleNamespace(
            route_type="telegram",
            conversation_id="100",
            thread_id="7",
            reply_to_message_id=None,
            metadata={},
        ),
        response_anchor=None,
        capability_snapshot=SimpleNamespace(
            capability_snapshot_id="caps",
            client_type="telegram",
            client_instance_id="bot-a",
        ),
    )

    class Reader:
        async def get_committed(self, input_batch_id):
            assert input_batch_id == "batch"
            return batch

    class CancelWake:
        async def __call__(self, session_id, emission_id):
            raise asyncio.CancelledError()

    context = ManagerToolExecutionContext(
        session_id="session",
        cycle_id="cycle",
        generation=0,
        context_revision_id=state.active_context_revision_id,
        tool_call_id="tool-call-1",
        original_input_batch_id="batch",
    )
    cancelled_service = AgentEmissionService(
        config=InputRuntimeConfigType(min_intermediate_message_interval_seconds=0),
        repository=repository.emissions,
        committed_batches=Reader(),
        clock=lambda: NOW,
        delivery_wake=CancelWake(),
    )
    with pytest.raises(asyncio.CancelledError):
        run(cancelled_service.emit_intermediate(
            context=context,
            message="durable before cancellation",
        ))
    durable = run(repository.emissions.list_pending_delivery())
    assert len(durable) == 1

    replay_service = AgentEmissionService(
        config=InputRuntimeConfigType(min_intermediate_message_interval_seconds=0),
        repository=repos(tmp_path).emissions,
        committed_batches=Reader(),
        clock=lambda: NOW,
    )
    replay = run(replay_service.emit_intermediate(
        context=context,
        message="durable before cancellation",
    ))
    assert replay["accepted"] is True
    assert replay["duplicate"] is True
    assert replay["emission_id"] == durable[0].emission_id
    assert len(run(repos(tmp_path).emissions.list_pending_delivery())) == 1


def test_failure_receipt_is_fenced_by_exact_cycle_generation_conversation_thread(tmp_path):
    repository, state = active(tmp_path)
    ready = run(repository.emissions.accept_intermediate(
        candidate(state),
        max_messages=10,
        min_interval_seconds=0,
    )).emission
    claimed = run(repository.emissions.claim_for_client(
        ready.emission_id,
        session_id="session",
        client_type="telegram",
        client_instance_id="bot-a",
        claim_token="claim",
        claimed_at=NOW,
        lease_seconds=30,
    ))
    wrong = [
        {"cycle_id": "other", "generation": 0, "conversation_id": "100", "thread_id": "7"},
        {"cycle_id": "cycle", "generation": 1, "conversation_id": "100", "thread_id": "7"},
        {"cycle_id": "cycle", "generation": 0, "conversation_id": "101", "thread_id": "7"},
        {"cycle_id": "cycle", "generation": 0, "conversation_id": "100", "thread_id": "8"},
    ]
    for route in wrong:
        with pytest.raises(PermissionError):
            run(repository.emissions.fail_for_route(
                claimed.emission_id,
                session_id="session",
                client_type="telegram",
                client_instance_id="bot-a",
                claim_token="claim",
                state="failed",
                error_code="rejected",
                **route,
            ))
    current = run(repository.emissions.get_by_idempotency_key("cycle", ready.idempotency_key))
    assert current.state == EmissionState.DELIVERING


def test_failed_intermediate_does_not_mutate_agent_cycle_state(tmp_path):
    repository, state = active(tmp_path)
    ready = run(repository.emissions.accept_intermediate(
        candidate(state),
        max_messages=10,
        min_interval_seconds=0,
    )).emission
    claimed = run(repository.emissions.claim_for_client(
        ready.emission_id,
        session_id="session",
        client_type="telegram",
        client_instance_id="bot-a",
        claim_token="claim",
        claimed_at=NOW,
        lease_seconds=30,
    ))
    failed = run(repository.emissions.fail_for_route(
        claimed.emission_id,
        session_id="session",
        cycle_id="cycle",
        generation=0,
        client_type="telegram",
        client_instance_id="bot-a",
        conversation_id="100",
        thread_id="7",
        claim_token="claim",
        state="failed",
        error_code="deterministic_rejection",
    ))
    after = run(repository.sessions.get("session"))
    assert failed.state == EmissionState.FAILED
    assert after == state
    assert after.cycle_status == CycleStatus.RUNNING
