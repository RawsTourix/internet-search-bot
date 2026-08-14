import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.input_runtime.config import InputRuntimeConfigType
from src.input_runtime.emissions import AgentEmissionService, ManagerToolExecutionContext
from src.input_runtime.models import CycleStatus, SessionInputRuntimeState, new_context_revision_id
from src.input_runtime.factory import create_filesystem_input_runtime_repositories
from src.storage import StorageConfigType


NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


class Clock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value


class Batches:
    def __init__(self, batch):
        self.batch = batch
        self.calls = 0

    async def get_committed(self, input_batch_id):
        self.calls += 1
        assert input_batch_id == self.batch.input_batch_id
        return self.batch


class Wake:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    async def __call__(self, session_id, emission_id):
        self.calls.append((session_id, emission_id))
        if self.fail:
            raise RuntimeError("wake failed")


def repositories(tmp_path):
    return create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )


def active_state(*, session="session", cycle="cycle", generation=0, context_revision_id=None, status=CycleStatus.RUNNING):
    return SessionInputRuntimeState(
        session_id=session,
        generation=generation,
        active_cycle_id=cycle,
        cycle_status=status,
        active_context_revision_id=context_revision_id or new_context_revision_id(),
        created_at=NOW,
        updated_at=NOW,
    )


def batch(*, session="session", input_batch_id="batch", client_type="telegram", instance="bot-a", conversation="100", thread="7", metadata=None):
    return SimpleNamespace(
        input_batch_id=input_batch_id,
        session_id=session,
        response_route=SimpleNamespace(
            route_type=client_type,
            conversation_id=conversation,
            thread_id=thread,
            reply_to_message_id="55",
            metadata=metadata or {"bot_token": "must-not-persist"},
        ),
        response_anchor=SimpleNamespace(
            anchor_id="anchor-1",
            client_message_id="56",
        ),
        capability_snapshot=SimpleNamespace(
            capability_snapshot_id="caps-1",
            client_type=client_type,
            client_instance_id=instance,
        ),
    )


def context(state, *, tool_call_id="call-1", input_batch_id="batch"):
    return ManagerToolExecutionContext(
        session_id=state.session_id,
        cycle_id=state.active_cycle_id,
        generation=state.generation,
        context_revision_id=state.active_context_revision_id,
        tool_call_id=tool_call_id,
        original_input_batch_id=input_batch_id,
    )


def service(tmp_path, *, config=None, clock=None, wake=None, state=None, committed=None):
    repos = repositories(tmp_path)
    state = state or active_state()
    run(repos.sessions.create_if_absent(state))
    committed = committed or batch(session=state.session_id)
    reader = Batches(committed)
    svc = AgentEmissionService(
        config=config or InputRuntimeConfigType(min_intermediate_message_interval_seconds=0),
        repository=repos.emissions,
        committed_batches=reader,
        clock=clock or Clock(),
        delivery_wake=wake,
    )
    return svc, repos, state, reader


def test_same_tool_call_replay_returns_same_emission_id(tmp_path):
    svc, _, state, _ = service(tmp_path)
    first = run(svc.emit_intermediate(context=context(state), message="Found a contradiction"))
    second = run(svc.emit_intermediate(context=context(state), message="Found a contradiction"))
    assert first["accepted"] is True
    assert second["emission_id"] == first["emission_id"]
    assert second["duplicate"] is True


def test_same_key_changed_semantics_is_conflict(tmp_path):
    svc, _, state, _ = service(tmp_path)
    run(svc.emit_intermediate(context=context(state), message="first", importance="normal"))
    changed = run(svc.emit_intermediate(context=context(state), message="second", importance="normal"))
    assert changed == {
        "type": "agent_emission_result",
        "accepted": False,
        "reason_code": "idempotency_conflict",
        "delivery_required_for_cycle": False,
    }


def test_concurrent_same_key_creates_one_record(tmp_path):
    svc, repos, state, _ = service(tmp_path)

    async def race():
        return await asyncio.gather(*[
            svc.emit_intermediate(context=context(state), message="same")
            for _ in range(12)
        ])

    results = run(race())
    assert len({item["emission_id"] for item in results}) == 1
    assert len(run(repos.emissions.list_pending_delivery())) == 1


def test_different_tool_calls_are_distinct(tmp_path):
    svc, _, state, _ = service(tmp_path)
    one = run(svc.emit_intermediate(context=context(state, tool_call_id="a"), message="one"))
    two = run(svc.emit_intermediate(context=context(state, tool_call_id="b"), message="two"))
    assert one["emission_id"] != two["emission_id"]


@pytest.mark.parametrize(
    ("message", "reason"),
    [("", "empty_message"), ("   \r\n ", "empty_message"), (123, "invalid_message")],
)
def test_invalid_message_rejected(tmp_path, message, reason):
    svc, _, state, _ = service(tmp_path)
    result = run(svc.emit_intermediate(context=context(state), message=message))
    assert result["accepted"] is False
    assert result["reason_code"] == reason


def test_max_chars_enforced_after_normalization(tmp_path):
    cfg = InputRuntimeConfigType(
        min_intermediate_message_interval_seconds=0,
        max_intermediate_message_chars=4,
    )
    svc, _, state, _ = service(tmp_path, config=cfg)
    assert run(svc.emit_intermediate(context=context(state), message=" 1234 "))["accepted"]
    assert run(svc.emit_intermediate(context=context(state, tool_call_id="b"), message="12345"))["reason_code"] == "message_too_long"


def test_max_per_cycle_counts_failed_delivery_budget(tmp_path):
    cfg = InputRuntimeConfigType(
        min_intermediate_message_interval_seconds=0,
        max_intermediate_messages_per_cycle=1,
    )
    svc, _, state, _ = service(tmp_path, config=cfg)
    assert run(svc.emit_intermediate(context=context(state, tool_call_id="a"), message="one"))["accepted"]
    rejected = run(svc.emit_intermediate(context=context(state, tool_call_id="b"), message="two"))
    assert rejected["reason_code"] == "max_per_cycle"


def test_min_interval_uses_fake_clock_without_sleep(tmp_path):
    clock = Clock()
    cfg = InputRuntimeConfigType(min_intermediate_message_interval_seconds=10)
    svc, _, state, _ = service(tmp_path, config=cfg, clock=clock)
    assert run(svc.emit_intermediate(context=context(state, tool_call_id="a"), message="one"))["accepted"]
    clock.value += timedelta(seconds=9)
    assert run(svc.emit_intermediate(context=context(state, tool_call_id="b"), message="two"))["reason_code"] == "rate_limited"
    clock.value += timedelta(seconds=1)
    assert run(svc.emit_intermediate(context=context(state, tool_call_id="c"), message="three"))["accepted"]


def test_concurrent_policy_race_cannot_exceed_count(tmp_path):
    cfg = InputRuntimeConfigType(
        min_intermediate_message_interval_seconds=0,
        max_intermediate_messages_per_cycle=3,
    )
    svc, repos, state, _ = service(tmp_path, config=cfg)

    async def race():
        return await asyncio.gather(*[
            svc.emit_intermediate(
                context=context(state, tool_call_id=f"call-{i}"),
                message=f"message-{i}",
            )
            for i in range(20)
        ])

    results = run(race())
    assert sum(bool(item["accepted"]) for item in results) == 3
    assert len(run(repos.emissions.list_pending_delivery())) == 3


def test_route_is_trusted_sanitized_and_original_batch_owned(tmp_path):
    committed = batch(metadata={"api_key": "secret", "callback_token": "secret"})
    svc, repos, state, _ = service(tmp_path, committed=committed)
    result = run(svc.emit_intermediate(
        context=context(state),
        message="semantic",
        kind="intermediate",
        importance="high",
    ))
    emission = run(repos.emissions.get_by_idempotency_key(
        state.active_cycle_id,
        AgentEmissionService.idempotency_key(context(state)),
    ))
    assert result["accepted"]
    assert emission.response_route == {
        "client_type": "telegram",
        "client_instance_id": "bot-a",
        "conversation_id": "100",
        "thread_id": "7",
        "reply_to_message_id": "56",
        "response_anchor_id": "anchor-1",
        "capability_snapshot_id": "caps-1",
    }
    assert "secret" not in repr(emission.response_route)


def test_missing_trusted_route_rejected_without_record(tmp_path):
    committed = batch()
    committed.response_route = None
    svc, repos, state, _ = service(tmp_path, committed=committed)
    result = run(svc.emit_intermediate(context=context(state), message="semantic"))
    assert result["reason_code"] == "route_unavailable"
    assert run(repos.emissions.list_pending_delivery()) == ()


def test_exact_context_revision_is_persisted_and_state_revision_unchanged(tmp_path):
    svc, repos, state, _ = service(tmp_path)
    before = run(repos.sessions.get(state.session_id))
    result = run(svc.emit_intermediate(context=context(state), message="semantic"))
    emission = run(repos.emissions.get_by_idempotency_key(
        state.active_cycle_id,
        AgentEmissionService.idempotency_key(context(state)),
    ))
    after = run(repos.sessions.get(state.session_id))
    assert result["accepted"]
    assert emission.context_revision_id == state.active_context_revision_id
    assert after == before
    assert after.cycle_status == CycleStatus.RUNNING


def test_stale_context_revision_rejected(tmp_path):
    svc, _, state, _ = service(tmp_path)
    stale = context(state)
    stale = ManagerToolExecutionContext(**{
        **stale.__dict__,
        "context_revision_id": new_context_revision_id(),
    }) if hasattr(stale, "__dict__") else ManagerToolExecutionContext(
        session_id=stale.session_id,
        cycle_id=stale.cycle_id,
        generation=stale.generation,
        context_revision_id=new_context_revision_id(),
        tool_call_id=stale.tool_call_id,
        original_input_batch_id=stale.original_input_batch_id,
    )
    assert run(svc.emit_intermediate(context=stale, message="semantic"))["reason_code"] == "context_revision_stale"


def test_terminal_cycle_rejects_new_emission(tmp_path):
    state = active_state(status=CycleStatus.DONE)
    svc, repos, state, _ = service(tmp_path, state=state)
    result = run(svc.emit_intermediate(context=context(state), message="too late"))
    assert result["reason_code"] == "cycle_terminal"
    assert run(repos.emissions.list_pending_delivery()) == ()


def test_wake_failure_after_ready_does_not_lose_intent(tmp_path):
    wake = Wake(fail=True)
    svc, repos, state, _ = service(tmp_path, wake=wake)
    result = run(svc.emit_intermediate(context=context(state), message="durable first"))
    assert result["accepted"] is True
    pending = run(repos.emissions.list_pending_delivery())
    assert len(pending) == 1 and pending[0].state.value == "ready"
    assert wake.calls == [(state.session_id, result["emission_id"])]


def test_replay_recovers_existing_before_route_resolution(tmp_path):
    svc, _, state, reader = service(tmp_path)
    first = run(svc.emit_intermediate(context=context(state), message="durable"))
    reader.batch.response_route = None
    second = run(svc.emit_intermediate(context=context(state), message="durable"))
    assert second["accepted"] is True
    assert second["emission_id"] == first["emission_id"]
    assert second["duplicate"] is True
