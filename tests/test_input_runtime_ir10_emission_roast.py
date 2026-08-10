from __future__ import annotations

import random
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.input_runtime.config import InputRuntimeConfigType
from src.input_runtime.emissions import (
    AgentEmissionDeliveryReceipt,
    AgentEmissionService,
    ManagerToolExecutionContext,
)
from src.input_runtime.errors import InputRuntimeConflictError
from src.input_runtime.factory import create_filesystem_input_runtime_repositories
from src.input_runtime.models import (
    CycleStatus,
    EmissionState,
    SessionInputRuntimeState,
    new_context_revision_id,
)
from src.storage import StorageConfigType
from tests.test_input_runtime_ir10_release import ACTIVE_SEEDS


START = datetime(2026, 8, 9, 0, 0, tzinfo=timezone.utc)
TRACE_LIMIT = 24


class Clock:
    def __init__(self) -> None:
        self.value = START

    def __call__(self) -> datetime:
        return self.value

    def tick(self, seconds: int = 1) -> datetime:
        self.value += timedelta(seconds=seconds)
        return self.value


class CommittedBatches:
    def __init__(self, session_ids: tuple[str, ...]) -> None:
        self.items = {
            session_id: SimpleNamespace(
                input_batch_id=f"initial-{session_id}",
                session_id=session_id,
                response_route=SimpleNamespace(
                    route_type="telegram",
                    conversation_id=f"conversation-{session_id}",
                    thread_id="7",
                    reply_to_message_id="55",
                    metadata={"bot_token": "must-not-persist"},
                ),
                response_anchor=SimpleNamespace(
                    anchor_id=f"anchor-{session_id}",
                    client_message_id="56",
                ),
                capability_snapshot=SimpleNamespace(
                    capability_snapshot_id=f"caps-{session_id}",
                    client_type="telegram",
                    client_instance_id="bot-ir10",
                ),
            )
            for session_id in session_ids
        }

    async def get_committed(self, input_batch_id: str):
        for item in self.items.values():
            if item.input_batch_id == input_batch_id:
                return item
        raise KeyError(input_batch_id)


class Wake:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, session_id: str, emission_id: str) -> None:
        self.calls.append((session_id, emission_id))


def repositories(root):
    return create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(root))
    )


def emission_service(repos, batches, clock, wake):
    return AgentEmissionService(
        config=InputRuntimeConfigType(
            min_intermediate_message_interval_seconds=0,
            max_intermediate_messages_per_cycle=1000,
        ),
        repository=repos.emissions,
        committed_batches=batches,
        clock=clock,
        delivery_wake=wake,
    )


def context(state: SessionInputRuntimeState, tool_call_id: str) -> ManagerToolExecutionContext:
    return ManagerToolExecutionContext(
        session_id=state.session_id,
        cycle_id=state.active_cycle_id,
        generation=state.generation,
        context_revision_id=state.active_context_revision_id,
        tool_call_id=tool_call_id,
        original_input_batch_id=f"initial-{state.session_id}",
    )


def receipt(record, *, token: str, external_id: str, delivered_at: datetime):
    return AgentEmissionDeliveryReceipt(
        emission_id=record.emission_id,
        session_id=record.session_id,
        cycle_id=record.cycle_id,
        generation=record.generation,
        claim_token=token,
        attempt_number=record.delivery_attempt_count,
        client_type="telegram",
        client_instance_id="bot-ir10",
        conversation_id=str(record.response_route["conversation_id"]),
        thread_id=str(record.response_route["thread_id"]),
        external_message_id=external_id,
        delivered_at=delivered_at,
    )


async def all_tracked(repos, tracked: dict[str, dict[str, tuple[str, str]]]):
    result = []
    for session_items in tracked.values():
        for cycle_id, key in session_items.values():
            record = await repos.emissions.get_by_idempotency_key(cycle_id, key)
            if record is not None:
                result.append(record)
    return result


async def assert_invariants(
    repos,
    *,
    session_ids: tuple[str, ...],
    tracked: dict[str, dict[str, tuple[str, str]]],
    identities: dict[tuple[str, str], str],
    trace: str,
) -> None:
    records = await all_tracked(repos, tracked)
    assert len({record.emission_id for record in records}) == len(records), trace

    for record in records:
        identity = (record.cycle_id, record.idempotency_key)
        expected_id = identities.setdefault(identity, record.emission_id)
        assert record.emission_id == expected_id, trace
        assert record.session_id in session_ids, trace
        assert record.response_route["client_type"] == "telegram", trace
        assert record.response_route["client_instance_id"] == "bot-ir10", trace
        assert record.response_route["conversation_id"] == (
            f"conversation-{record.session_id}"
        ), trace
        assert "bot_token" not in repr(record.response_route), trace

        if record.state == EmissionState.DELIVERING:
            assert record.delivery_claim_token, trace
            assert record.delivery_attempt_count >= 1, trace
        else:
            assert record.delivery_claim_token is None, trace

        if record.state in {
            EmissionState.UNKNOWN,
            EmissionState.FAILED,
            EmissionState.DELIVERED,
            EmissionState.CANCELLED,
        }:
            ready = await repos.emissions.list_ready_for_client(
                client_type="telegram",
                client_instance_id="bot-ir10",
                limit=1000,
                now=START + timedelta(days=2),
            )
            assert record.emission_id not in {item.emission_id for item in ready}, trace

        state = await repos.sessions.get(record.session_id)
        assert state is not None, trace
        if record.generation < state.generation:
            assert record.state not in {
                EmissionState.READY,
                EmissionState.DELIVERING,
            }, trace

    pending = await repos.emissions.list_pending_delivery()
    pending_ids = {record.emission_id for record in pending}
    for record in records:
        if record.state in {
            EmissionState.UNKNOWN,
            EmissionState.FAILED,
            EmissionState.DELIVERED,
            EmissionState.CANCELLED,
        }:
            assert record.emission_id not in pending_ids, trace


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", ACTIVE_SEEDS)
async def test_ir10_seeded_emission_delivery_state_machine(seed, tmp_path):
    rng = random.Random(seed ^ 0xE11A10)
    session_ids = ("session-a", "session-b", "session-c")
    clock = Clock()
    batches = CommittedBatches(session_ids)
    wake = Wake()
    repos = repositories(tmp_path)
    service = emission_service(repos, batches, clock, wake)
    states: dict[str, SessionInputRuntimeState] = {}
    tracked: dict[str, dict[str, tuple[str, str]]] = defaultdict(dict)
    semantics: dict[str, dict[str, str]] = defaultdict(dict)
    identities: dict[tuple[str, str], str] = {}
    trace: deque[str] = deque(maxlen=TRACE_LIMIT)

    for session_id in session_ids:
        state = SessionInputRuntimeState(
            session_id=session_id,
            generation=0,
            active_cycle_id=f"cycle-{session_id}-g0",
            cycle_status=CycleStatus.RUNNING,
            active_context_revision_id=new_context_revision_id(),
            created_at=START,
            updated_at=START,
        )
        await repos.sessions.create_if_absent(state)
        states[session_id] = state

    steps = int(__import__("os").environ.get("IR10_EMISSION_STEPS", "120"))
    counters = defaultdict(int)

    for operation_index in range(steps):
        session_id = rng.choice(session_ids)
        state = await repos.sessions.get(session_id)
        states[session_id] = state
        records = [
            record
            for record in await all_tracked(repos, tracked)
            if record.session_id == session_id
        ]
        ready = [record for record in records if record.state == EmissionState.READY]
        delivering = [
            record for record in records if record.state == EmissionState.DELIVERING
        ]
        choice = rng.randrange(100)
        operation = "emit"

        try:
            if choice < 30 or not records:
                tool_call_id = f"tool-{session_id}-{counters[(session_id, 'tool')]}"
                counters[(session_id, "tool")] += 1
                message = f"semantic-{seed}-{session_id}-{tool_call_id}"
                operation = f"emit:{tool_call_id}"
                result = await service.emit_intermediate(
                    context=context(state, tool_call_id),
                    message=message,
                )
                assert result["accepted"] is True
                key = AgentEmissionService.idempotency_key(context(state, tool_call_id))
                tracked[session_id][tool_call_id] = (state.active_cycle_id, key)
                semantics[session_id][tool_call_id] = message

            elif choice < 40 and tracked[session_id]:
                tool_call_id = rng.choice(tuple(tracked[session_id]))
                cycle_id, key = tracked[session_id][tool_call_id]
                record = await repos.emissions.get_by_idempotency_key(cycle_id, key)
                if record is not None and record.generation == state.generation:
                    operation = f"replay:{tool_call_id}"
                    replay = await service.emit_intermediate(
                        context=context(state, tool_call_id),
                        message=semantics[session_id][tool_call_id],
                    )
                    assert replay["accepted"] is True
                    assert replay["emission_id"] == record.emission_id
                    assert replay["duplicate"] is True

            elif choice < 58 and ready:
                record = rng.choice(ready)
                token = f"claim-{seed}-{operation_index}"
                operation = f"claim:{record.emission_id}"
                claimed = await repos.emissions.claim_for_client(
                    record.emission_id,
                    session_id=record.session_id,
                    client_type="telegram",
                    client_instance_id="bot-ir10",
                    claim_token=token,
                    claimed_at=clock.tick(),
                    lease_seconds=30,
                )
                assert claimed.state == EmissionState.DELIVERING

            elif choice < 67 and delivering:
                record = rng.choice(delivering)
                operation = f"competing-claim:{record.emission_id}"
                with pytest.raises(InputRuntimeConflictError):
                    await repos.emissions.claim_for_client(
                        record.emission_id,
                        session_id=record.session_id,
                        client_type="telegram",
                        client_instance_id="bot-ir10",
                        claim_token=f"rival-{seed}-{operation_index}",
                        claimed_at=clock.tick(),
                        lease_seconds=30,
                    )

            elif choice < 77 and delivering:
                record = rng.choice(delivering)
                token = record.delivery_claim_token
                operation = f"receipt:{record.emission_id}"
                evidence = receipt(
                    record,
                    token=token,
                    external_id=f"external-{record.emission_id}",
                    delivered_at=clock.tick(),
                )
                first = await repos.emissions.record_delivery_receipt(evidence)
                reopened = repositories(tmp_path)
                second = await reopened.emissions.record_delivery_receipt(evidence)
                assert first == second
                repos = reopened
                service = emission_service(repos, batches, clock, wake)

            elif choice < 84 and delivering:
                record = rng.choice(delivering)
                operation = f"unknown:{record.emission_id}"
                changed = await repos.emissions.fail_for_client(
                    record.emission_id,
                    session_id=record.session_id,
                    client_type="telegram",
                    client_instance_id="bot-ir10",
                    claim_token=record.delivery_claim_token,
                    state="unknown",
                    error_code="synthetic_ambiguous_delivery",
                )
                assert changed.state == EmissionState.UNKNOWN

            elif choice < 90 and delivering:
                record = rng.choice(delivering)
                operation = f"failed:{record.emission_id}"
                changed = await repos.emissions.fail_for_client(
                    record.emission_id,
                    session_id=record.session_id,
                    client_type="telegram",
                    client_instance_id="bot-ir10",
                    claim_token=record.delivery_claim_token,
                    state="failed",
                    error_code="synthetic_rejected_delivery",
                )
                assert changed.state == EmissionState.FAILED

            elif choice < 95:
                operation = "reopen-expire"
                repos = repositories(tmp_path)
                expired = await repos.emissions.recover_expired_delivery_claims(
                    now=clock.tick(31)
                )
                assert all(item.state == EmissionState.UNKNOWN for item in expired)
                service = emission_service(repos, batches, clock, wake)

            else:
                operation = "reset-generation"
                old_generation = state.generation
                await repos.emissions.cancel_generation(
                    session_id,
                    generation=old_generation,
                    reason_code="ir10_random_reset",
                )
                next_generation = old_generation + 1
                current = await repos.sessions.get(session_id)
                changed = current.model_copy(
                    update={
                        "generation": next_generation,
                        "active_cycle_id": f"cycle-{session_id}-g{next_generation}",
                        "cycle_status": CycleStatus.RUNNING,
                        "active_context_revision_id": new_context_revision_id(),
                        "revision": current.revision + 1,
                        "updated_at": clock.tick(),
                    }
                )
                state = await repos.sessions.compare_and_swap(
                    current.revision,
                    changed,
                )
                states[session_id] = state

            trace.append(f"{operation_index}:{session_id}:{operation}")
            current = await repos.sessions.get(session_id)
            trace_text = (
                f"seed={seed}; operation_index={operation_index}; "
                f"operation={operation}; session={session_id}; "
                f"generation={current.generation}; cycle={current.active_cycle_id}; "
                f"last_operations={list(trace)!r}"
            )
            await assert_invariants(
                repos,
                session_ids=session_ids,
                tracked=tracked,
                identities=identities,
                trace=trace_text,
            )
        except Exception as error:
            pytest.fail(
                f"IR-10 emission randomized failure: seed={seed}; "
                f"operation_index={operation_index}; operation={operation}; "
                f"session={session_id}; last_operations={list(trace)!r}; "
                f"error={type(error).__name__}: {error}",
                pytrace=True,
            )
