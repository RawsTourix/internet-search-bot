from __future__ import annotations

import json
import os
import random
import subprocess
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.input_runtime import (
    CheckpointAction,
    CheckpointName,
    ControlState,
    CycleStatus,
    InputAdmissionAction,
    InputAdmissionService,
    InputRuntimeConfigType,
    create_filesystem_input_runtime_repositories,
)
from src.input_runtime.diagnostics import InputRuntimeDiagnosticsService
from src.input_runtime.ir9_filesystem import FileSystemRuntimeDiagnosticsReader
from src.input_runtime.recovery import InputRuntimeReadinessGate, RecoveryDisposition
from src.input_runtime.recovery_terminal import InputRuntimeRecoveryCoordinator
from src.runtime import ActiveAgentCycle, SessionExecutionCoordinator
from src.runtime.input_runtime_rehydration import rehydrate_active_agent_cycle
from src.storage import StorageConfigType


# Deliberately far from wall-clock time. Some production snapshot helpers stamp their
# own creation time before the injected runtime clock is used for later mutations.
NOW = datetime(2030, 1, 1, 0, 0, tzinfo=timezone.utc)
CI_SEEDS = (20260810, 104729, 8675309, 424242)
RELEASE_SEEDS = CI_SEEDS + (
    1,
    7,
    42,
    271828,
    314159,
    1618033,
    999983,
    2147483647,
)
ACTIVE_SEEDS = (
    RELEASE_SEEDS
    if os.environ.get("IR10_SEED_SET") == "release"
    else CI_SEEDS
)
TRACE_LIMIT = 24


@dataclass
class TextPart:
    part_id: str
    kind: str
    text: str
    attachment_slot_ids: list[str] = field(default_factory=list)


@dataclass
class Batch:
    input_batch_id: str
    session_id: str
    sequence_number: int
    payload_size: int = 10
    text_parts: list[TextPart] = field(default_factory=list)
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

    def add(self, batch: Batch) -> None:
        self.batches[batch.input_batch_id] = batch

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


class Wake:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def wake(self, session_id: str, *, cycle_id: str) -> bool:
        self.calls.append((session_id, cycle_id))
        return True


def repositories(root):
    return create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(root))
    )


def runtime(
    root,
    reader: Reader,
    *,
    config: InputRuntimeConfigType | None = None,
    repository_bundle=None,
    cycle_prefix: str = "cycle",
):
    repos = repository_bundle or repositories(root)
    coordinator = SessionExecutionCoordinator()
    wake = Wake()
    counter = iter(range(1, 100000))
    service = InputAdmissionService(
        config=config
        or InputRuntimeConfigType(min_intermediate_message_interval_seconds=0),
        repositories=repos,
        committed_batches=reader,
        wake_coordinator=wake,
        cycle_id_factory=lambda: f"{cycle_prefix}-{next(counter)}",
        clock=lambda: NOW,
        payload_size_resolver=lambda batch: batch.payload_size,
    )
    return repos, coordinator, wake, service


def active_cycle(
    *,
    session_id: str,
    cycle_id: str,
    input_batch_id: str,
    generation: int,
) -> ActiveAgentCycle:
    return ActiveAgentCycle(
        cycle_id=cycle_id,
        session_id=session_id,
        original_user_request="synthetic initial",
        messages_for_llm=[
            {"role": "system", "content": "system"},
            {
                "role": "user",
                "content": json.dumps(
                    {"type": "user_request", "user_request": "synthetic initial"}
                ),
            },
        ],
        cycle_trace=[],
        original_user_message_index=1,
        original_input_batch_id=input_batch_id,
        input_runtime_generation=generation,
    )


def diagnostics(repos):
    return InputRuntimeDiagnosticsService(
        FileSystemRuntimeDiagnosticsReader(
            root=repos.coordination_root,
            locks=repos.coordination_locks,
        ),
        clock=lambda: NOW,
    )


async def seed_cycle(service: InputAdmissionService, batch: Batch):
    admitted = await service.admit_committed_batch(
        batch.input_batch_id,
        session_id=batch.session_id,
    )
    assert admitted.action == InputAdmissionAction.START_CYCLE
    state = await service.repositories.sessions.get(batch.session_id)
    cycle = active_cycle(
        session_id=batch.session_id,
        cycle_id=admitted.target_cycle_id,
        input_batch_id=batch.input_batch_id,
        generation=state.generation,
    )
    outcome = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.RESUME,
        active_cycle=cycle,
        desired_status=CycleStatus.RUNNING,
    )
    assert outcome.action in {
        CheckpointAction.CONTINUE,
        CheckpointAction.INPUT_APPLIED,
    }
    return admitted, cycle


async def recover(root, reader: Reader):
    repos, coordinator, _, service = runtime(
        root,
        reader,
        cycle_prefix="recovery-must-not-allocate",
    )
    gate = InputRuntimeReadinessGate()
    recovery = InputRuntimeRecoveryCoordinator(
        repositories=repos,
        admission_service=service,
        committed_batches=reader,
        readiness_gate=gate,
        generation_coordinator=coordinator,
        clock=lambda: NOW,
    )
    plan = await recovery.recover()
    return repos, coordinator, service, gate, plan


def input_updates(cycle: ActiveAgentCycle) -> list[dict]:
    result: list[dict] = []
    for message in cycle.messages_for_llm:
        if message.get("role") != "user" or not isinstance(message.get("content"), str):
            continue
        try:
            payload = json.loads(message["content"])
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("type") == "input_batch_update":
            result.append(payload)
    return result


def assert_tool_protocol(messages: list[dict]) -> None:
    calls: dict[str, int] = {}
    results: dict[str, int] = defaultdict(int)
    for message in messages:
        if message.get("role") == "assistant":
            for tool_call in message.get("tool_calls") or ():
                call_id = tool_call.get("id")
                assert call_id and call_id not in calls
                calls[call_id] = 1
        elif message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            assert call_id in calls, f"orphan tool result {call_id!r}"
            results[call_id] += 1
            assert results[call_id] == 1, f"duplicate tool result {call_id!r}"
    assert set(calls) == set(results), (
        f"open tool block calls={sorted(calls)} results={sorted(results)}"
    )


@pytest.mark.asyncio
async def test_ir10_running_burst_is_same_cycle_fifo_bounded_and_status_read_only(tmp_path):
    batches = [
        Batch("initial", "session", 1),
        Batch("a1", "session", 2, text_parts=[TextPart("p1", "message_text", "one")]),
        Batch("a2", "session", 3, text_parts=[TextPart("p2", "message_text", "two")]),
        Batch("a3", "session", 4, text_parts=[TextPart("p3", "message_text", "three")]),
    ]
    reader = Reader(*batches)
    config = InputRuntimeConfigType(
        max_queued_batches_per_session=8,
        max_queued_bytes_per_session=1000,
        max_batches_per_checkpoint=2,
        max_batch_bytes_per_checkpoint=1000,
        min_intermediate_message_interval_seconds=0,
    )
    repos, _, wake, service = runtime(
        tmp_path,
        reader,
        config=config,
        cycle_prefix="burst",
    )
    initial, cycle = await seed_cycle(service, batches[0])
    cycle_id = initial.target_cycle_id

    # Closed multi-tool block is durable protocol context before the additions.
    cycle.messages_for_llm.extend(
        [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "tool-1", "type": "function", "function": {"name": "one", "arguments": "{}"}},
                    {"id": "tool-2", "type": "function", "function": {"name": "two", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "tool-1", "content": '{"ok":true}'},
            {"role": "tool", "tool_call_id": "tool-2", "content": '{"ok":true}'},
        ]
    )

    for batch in batches[1:]:
        outcome = await service.admit_committed_batch(
            batch.input_batch_id,
            session_id="session",
        )
        assert outcome.action == InputAdmissionAction.QUEUED_RUNNING
        assert outcome.target_cycle_id == cycle_id
        assert outcome.should_start_runner is False

    before = await repos.sessions.get("session")
    before_rows = await repos.admissions.list_for_session("session")
    view = await diagnostics(repos).status("session")
    assert await repos.sessions.get("session") == before
    assert await repos.admissions.list_for_session("session") == before_rows
    assert view.active_cycle_id == cycle_id
    assert view.input.queued == 3

    applied = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.AFTER_TOOL_BLOCK,
        active_cycle=cycle,
        desired_status=CycleStatus.RUNNING,
    )
    assert applied.action == CheckpointAction.INPUT_APPLIED
    assert applied.applied_input_batch_ids == ("a1", "a2", "a3")
    assert applied.applied_through_cycle_sequence == 3

    admissions = await repos.admissions.list_for_session("session")
    assert [row.session_sequence for row in admissions] == [1, 2, 3, 4]
    assert [row.cycle_sequence for row in admissions] == [0, 1, 2, 3]
    assert {row.target_cycle_id for row in admissions} == {cycle_id}
    assert [row.input_batch_id for row in await repos.inbox.list_for_cycle(cycle_id)] == [
        "a1",
        "a2",
        "a3",
    ]
    # max_batches_per_checkpoint bounds each durable revision, not the whole
    # accepted-at-entry drain. Three additions therefore require two updates.
    assert len(input_updates(cycle)) == 2
    assert all(call == ("session", cycle_id) for call in wake.calls)
    assert_tool_protocol(cycle.messages_for_llm)


@pytest.mark.asyncio
async def test_ir10_pause_continue_freezes_target_and_late_input_waits(tmp_path):
    batches = [
        Batch("initial", "session", 1),
        Batch("before", "session", 2, text_parts=[TextPart("p1", "message_text", "before")]),
        Batch("after", "session", 3, text_parts=[TextPart("p2", "message_text", "after")]),
    ]
    reader = Reader(*batches)
    repos, _, _, service = runtime(
        tmp_path,
        reader,
        config=InputRuntimeConfigType(
            max_queued_batches_per_session=8,
            max_queued_bytes_per_session=1000,
            max_batches_per_checkpoint=1,
            max_batch_bytes_per_checkpoint=1000,
            min_intermediate_message_interval_seconds=0,
        ),
        cycle_prefix="pause",
    )
    initial, cycle = await seed_cycle(service, batches[0])
    cycle_id = initial.target_cycle_id

    pause = await service.control_service.request_pause(
        session_id="session",
        idempotency_key="ir10-pause",
        source_client_type="test",
    )
    assert pause.command.state == ControlState.ACKNOWLEDGED
    await service.admit_committed_batch("before", session_id="session")
    paused = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.BEFORE_LLM,
        active_cycle=cycle,
        desired_status=CycleStatus.RUNNING,
    )
    assert paused.action == CheckpointAction.PAUSE
    assert (await repos.sessions.get("session")).cycle_status == CycleStatus.PAUSED_BY_USER
    assert (await repos.inbox.list_for_cycle(cycle_id))[0].state.value == "queued"

    continued = await service.control_service.request_continue(
        session_id="session",
        idempotency_key="ir10-continue",
        source_client_type="test",
    )
    assert service.control_service.resume_input_target(continued.command) == 1

    late = await service.admit_committed_batch("after", session_id="session")
    assert late.target_cycle_id == cycle_id
    resumed = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.RESUME,
        active_cycle=cycle,
        desired_status=CycleStatus.RUNNING,
    )
    assert resumed.applied_input_batch_ids == ("before",)
    rows = await repos.inbox.list_for_cycle(cycle_id)
    assert [(row.input_batch_id, row.state.value) for row in rows] == [
        ("before", "applied"),
        ("after", "queued"),
    ]

    next_checkpoint = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.BEFORE_LLM,
        active_cycle=cycle,
        desired_status=CycleStatus.RUNNING,
    )
    assert next_checkpoint.applied_input_batch_ids == ("after",)
    state = await repos.sessions.get("session")
    assert state.active_cycle_id == cycle_id
    assert state.active_cycle_accepted_through_sequence == 2
    assert state.active_cycle_applied_through_sequence == 2
    assert state.cycle_status == CycleStatus.RUNNING


@pytest.mark.asyncio
async def test_ir10_waiting_restart_then_new_reply_keeps_same_cycle_and_fifo(tmp_path):
    initial = Batch("initial", "session", 1)
    queued = Batch("queued", "session", 2, text_parts=[TextPart("p1", "message_text", "queued")])
    reply = Batch("reply", "session", 3, text_parts=[TextPart("p2", "message_text", "reply")])
    reader = Reader(initial, queued)
    repos, _, _, service = runtime(tmp_path, reader, cycle_prefix="waiting")
    admitted, cycle = await seed_cycle(service, initial)
    cycle_id = admitted.target_cycle_id
    await service.admit_committed_batch("queued", session_id="session")

    snapshot = await repos.snapshots.get(cycle_id)
    waiting_snapshot = snapshot.model_copy(
        update={
            "status": CycleStatus.WAITING_USER,
            "waiting_question": "Which option?",
            "safe_checkpoint": CheckpointName.BEFORE_WAITING,
            "snapshot_revision": snapshot.snapshot_revision + 1,
            "updated_at": NOW,
        }
    )
    await repos.snapshots.compare_and_swap(snapshot.snapshot_revision, waiting_snapshot)
    state = await repos.sessions.get("session")
    await repos.sessions.compare_and_swap(
        state.revision,
        state.model_copy(
            update={
                "cycle_status": CycleStatus.WAITING_USER,
                "revision": state.revision + 1,
                "updated_at": NOW,
            }
        ),
    )

    fresh, _, fresh_service, _, plan = await recover(tmp_path, reader)
    recovered = next(item for item in plan.sessions if item.session_id == "session")
    assert recovered.disposition == RecoveryDisposition.WAITING
    assert recovered.cycle_id == cycle_id
    assert recovered.snapshot.waiting_question == "Which option?"

    # This user reply did not exist when the crashed process disappeared.
    reader.add(reply)
    rehydrated = rehydrate_active_agent_cycle(recovered.snapshot)
    admitted_reply = await fresh_service.admit_committed_batch(
        "reply",
        session_id="session",
    )
    assert admitted_reply.action == InputAdmissionAction.RESUME_WAITING
    assert admitted_reply.target_cycle_id == cycle_id
    resumed = await fresh_service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.RESUME,
        active_cycle=rehydrated,
        desired_status=CycleStatus.RUNNING,
    )
    assert resumed.applied_input_batch_ids == ("queued", "reply")
    assert resumed.applied_through_cycle_sequence == 2
    assert [row.input_batch_id for row in await fresh.inbox.list_for_cycle(cycle_id)] == [
        "queued",
        "reply",
    ]
    assert (await fresh.sessions.get("session")).active_cycle_id == cycle_id


@pytest.mark.asyncio
async def test_ir10_reset_crash_restart_finishes_once_and_old_generation_stays_fenced(tmp_path, monkeypatch):
    initial = Batch("initial", "session", 1)
    old_addition = Batch("old-addition", "session", 2)
    new_input = Batch("new-input", "session", 3)
    reader = Reader(initial, old_addition, new_input)
    repos, _, _, service = runtime(tmp_path, reader, cycle_prefix="reset")
    admitted, _ = await seed_cycle(service, initial)
    old_cycle = admitted.target_cycle_id
    await service.admit_committed_batch("old-addition", session_id="session")

    real_cancel = repos.snapshots.cancel_generation

    async def crash_cleanup(*args, **kwargs):
        raise OSError("ir10 reset cleanup crash")

    monkeypatch.setattr(repos.snapshots, "cancel_generation", crash_cleanup)
    with pytest.raises(OSError, match="ir10 reset cleanup crash"):
        await service.control_service.request_reset(
            session_id="session",
            idempotency_key="ir10-reset",
            source_client_type="test",
        )
    monkeypatch.setattr(repos.snapshots, "cancel_generation", real_cancel)

    crashed = await repos.sessions.get("session")
    assert crashed.generation == 1
    assert crashed.active_cycle_id is None

    fresh, _, fresh_service, _, plan = await recover(tmp_path, reader)
    assert plan.sessions == ()
    state = await fresh.sessions.get("session")
    assert state.generation == 1
    assert state.cycle_status == CycleStatus.IDLE
    assert (await fresh.snapshots.get(old_cycle)).status == CycleStatus.CANCELLED

    fresh_service.cycle_id_factory = lambda: "reset-new-cycle"
    new = await fresh_service.admit_committed_batch("new-input", session_id="session")
    assert new.action == InputAdmissionAction.START_CYCLE
    assert new.target_cycle_id == "reset-new-cycle"
    assert new.admission.admitted_generation == 1
    assert new.target_cycle_id != old_cycle
    rows = await fresh.admissions.list_for_session("session")
    assert next(row for row in rows if row.input_batch_id == "old-addition").state.value == "cancelled"


@pytest.mark.asyncio
async def test_ir10_capacity_exact_limit_duplicate_costs_zero_and_plus_one_is_blocked(tmp_path):
    batches = [
        Batch("initial", "session", 1, payload_size=1),
        Batch("exact-a", "session", 2, payload_size=5),
        Batch("exact-b", "session", 3, payload_size=5),
        Batch("plus-one", "session", 4, payload_size=1),
    ]
    reader = Reader(*batches)
    repos, _, _, service = runtime(
        tmp_path,
        reader,
        config=InputRuntimeConfigType(
            max_queued_batches_per_session=2,
            max_queued_bytes_per_session=10,
            max_batches_per_checkpoint=1,
            max_batch_bytes_per_checkpoint=5,
            min_intermediate_message_interval_seconds=0,
        ),
        cycle_prefix="capacity",
    )
    admitted, cycle = await seed_cycle(service, batches[0])
    cycle_id = admitted.target_cycle_id
    one = await service.admit_committed_batch("exact-a", session_id="session")
    two = await service.admit_committed_batch("exact-b", session_id="session")
    assert one.action == two.action == InputAdmissionAction.QUEUED_RUNNING
    assert (await service.admit_committed_batch("exact-a", session_id="session")).action == InputAdmissionAction.DUPLICATE

    blocked = await service.admit_committed_batch("plus-one", session_id="session")
    assert blocked.action == InputAdmissionAction.CAPACITY_BLOCKED
    assert blocked.reason_code in {
        "max_queued_batches_per_session",
        "max_queued_bytes_per_session",
    }
    assert await repos.admissions.get_by_input_batch_id("plus-one") is None

    drained = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.BEFORE_LLM,
        active_cycle=cycle,
        desired_status=CycleStatus.RUNNING,
    )
    assert drained.applied_input_batch_ids == ("exact-a", "exact-b")
    assert len(input_updates(cycle)) == 2
    assert [row.cycle_sequence for row in await repos.inbox.list_for_cycle(cycle_id)] == [1, 2]


async def assert_invariants(repos, session_ids: tuple[str, ...], *, trace: str) -> None:
    global_admissions: set[str] = set()
    global_inputs: set[str] = set()
    current_cycles: list[str] = []

    for session_id in session_ids:
        state = await repos.sessions.get(session_id)
        assert state is not None, trace
        assert state.active_cycle_applied_through_sequence <= state.active_cycle_accepted_through_sequence, trace
        assert state.applied_control_sequence <= state.pending_control_sequence, trace

        admissions = await repos.admissions.list_for_session(session_id)
        sequences = [row.session_sequence for row in admissions]
        if sequences:
            assert sequences == list(range(1, len(sequences) + 1)), trace
        assert len({row.input_batch_id for row in admissions}) == len(admissions), trace
        assert len({row.admission_id for row in admissions}) == len(admissions), trace
        for row in admissions:
            assert row.session_id == session_id, trace
            assert row.admission_id not in global_admissions, trace
            assert row.input_batch_id not in global_inputs, trace
            global_admissions.add(row.admission_id)
            global_inputs.add(row.input_batch_id)

        by_cycle: dict[tuple[int, str], list[object]] = defaultdict(list)
        for row in admissions:
            by_cycle[(row.admitted_generation, row.target_cycle_id)].append(row)
        for identity, rows in by_cycle.items():
            cycle_sequences = sorted(row.cycle_sequence for row in rows)
            assert cycle_sequences == list(range(len(cycle_sequences))), (
                f"{trace}; identity={identity}; cycle_sequences={cycle_sequences}"
            )

        controls = await repos.controls.list_for_session(session_id)
        control_sequences = [row.sequence_number for row in controls]
        if control_sequences:
            assert control_sequences == list(range(1, len(control_sequences) + 1)), trace
            keys = [row.idempotency_key for row in controls]
            assert len(keys) == len(set(keys)), trace

        if state.active_cycle_id is not None:
            current_cycles.append(state.active_cycle_id)
            snapshot = await repos.snapshots.get(state.active_cycle_id)
            if snapshot is not None:
                assert snapshot.session_id == session_id, trace
                assert snapshot.generation == state.generation, trace
                assert snapshot.applied_through_cycle_sequence <= state.active_cycle_accepted_through_sequence, trace
                assert_tool_protocol(snapshot.messages_for_llm)

        durable_before_status = state
        view = await diagnostics(repos).status(session_id)
        assert await repos.sessions.get(session_id) == durable_before_status, (
            f"{trace}; diagnostics mutated runtime"
        )
        assert view.generation == state.generation, trace
        assert view.active_cycle_id == state.active_cycle_id, trace

    assert len(current_cycles) == len(set(current_cycles)), trace


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", ACTIVE_SEEDS)
async def test_ir10_seeded_multisession_state_machine(seed, tmp_path):
    rng = random.Random(seed)
    session_ids = ("session-a", "session-b", "session-c")
    queues: dict[str, deque[Batch]] = {}
    batches: list[Batch] = []
    for session_index, session_id in enumerate(session_ids):
        session_batches = [
            Batch(
                f"{session_id}-batch-{index}",
                session_id,
                index + 1,
                payload_size=1 + ((seed + session_index + index) % 9),
                text_parts=[
                    TextPart(
                        f"part-{session_index}-{index}",
                        "message_text",
                        f"synthetic-{session_index}-{index}",
                    )
                ],
                # Same transport-shaped ID is intentionally valid in different
                # session scopes; input_batch_id remains globally unique.
                source_event_ids=(f"evt_{index:032x}",),
            )
            for index in range(80)
        ]
        queues[session_id] = deque(session_batches)
        batches.extend(session_batches)

    reader = Reader(*batches)
    config = InputRuntimeConfigType(
        max_queued_batches_per_session=8,
        max_queued_bytes_per_session=64,
        max_batches_per_checkpoint=3,
        max_batch_bytes_per_checkpoint=24,
        min_intermediate_message_interval_seconds=0,
    )
    repos, _, _, service = runtime(
        tmp_path,
        reader,
        config=config,
        cycle_prefix=f"seed-{seed}",
    )
    active: dict[str, ActiveAgentCycle] = {}
    admitted_ids: dict[str, list[str]] = defaultdict(list)
    trace: deque[str] = deque(maxlen=TRACE_LIMIT)

    for session_id in session_ids:
        first = queues[session_id].popleft()
        outcome, cycle = await seed_cycle(service, first)
        active[session_id] = cycle
        admitted_ids[session_id].append(first.input_batch_id)
        trace.append(f"bootstrap:{session_id}:{outcome.target_cycle_id}")

    steps = int(os.environ.get("IR10_RANDOM_STEPS", "180"))
    for operation_index in range(steps):
        session_id = rng.choice(session_ids)
        state = await repos.sessions.get(session_id)
        choice = rng.randrange(100)
        operation = "status"
        try:
            if choice < 36 and queues[session_id]:
                batch = queues[session_id].popleft()
                operation = f"admit:{batch.input_batch_id}"
                outcome = await service.admit_committed_batch(
                    batch.input_batch_id,
                    session_id=session_id,
                )
                if outcome.action == InputAdmissionAction.CAPACITY_BLOCKED:
                    queues[session_id].appendleft(batch)
                else:
                    admitted_ids[session_id].append(batch.input_batch_id)
                    if outcome.action == InputAdmissionAction.START_CYCLE:
                        active[session_id] = active_cycle(
                            session_id=session_id,
                            cycle_id=outcome.target_cycle_id,
                            input_batch_id=batch.input_batch_id,
                            generation=outcome.admission.admitted_generation,
                        )
                        await service.checkpoint_service.run_checkpoint(
                            checkpoint=CheckpointName.RESUME,
                            active_cycle=active[session_id],
                            desired_status=CycleStatus.RUNNING,
                        )

            elif choice < 46 and admitted_ids[session_id]:
                batch_id = rng.choice(admitted_ids[session_id])
                operation = f"duplicate:{batch_id}"
                duplicate = await service.admit_committed_batch(
                    batch_id,
                    session_id=session_id,
                )
                assert duplicate.action == InputAdmissionAction.DUPLICATE

            elif choice < 62 and state.active_cycle_id is not None and session_id in active:
                if state.cycle_status in {CycleStatus.RUNNING, CycleStatus.PAUSE_REQUESTED}:
                    operation = "checkpoint"
                    await service.checkpoint_service.run_checkpoint(
                        checkpoint=CheckpointName.BEFORE_LLM,
                        active_cycle=active[session_id],
                        desired_status=CycleStatus.RUNNING,
                    )

            elif choice < 70 and state.cycle_status == CycleStatus.RUNNING:
                operation = "pause"
                await service.control_service.request_pause(
                    session_id=session_id,
                    idempotency_key=f"{seed}:{operation_index}:pause",
                    source_client_type="ir10",
                )

            elif choice < 77 and state.cycle_status in {
                CycleStatus.PAUSED_BY_USER,
                CycleStatus.PAUSE_REQUESTED,
            }:
                operation = "continue"
                await service.control_service.request_continue(
                    session_id=session_id,
                    idempotency_key=f"{seed}:{operation_index}:continue",
                    source_client_type="ir10",
                )
                if session_id in active:
                    await service.checkpoint_service.run_checkpoint(
                        checkpoint=CheckpointName.RESUME,
                        active_cycle=active[session_id],
                        desired_status=CycleStatus.RUNNING,
                    )

            elif choice < 84 and state.active_cycle_id is not None:
                operation = "reset"
                await service.control_service.request_reset(
                    session_id=session_id,
                    idempotency_key=f"{seed}:{operation_index}:reset",
                    source_client_type="ir10",
                )
                active.pop(session_id, None)

            elif choice < 92:
                operation = "repository-reopen"
                repos, _, _, service = runtime(
                    tmp_path,
                    reader,
                    config=config,
                    cycle_prefix=f"seed-{seed}-reopen-{operation_index}",
                )
                for candidate_session in session_ids:
                    current = await repos.sessions.get(candidate_session)
                    if current is None or current.active_cycle_id is None:
                        active.pop(candidate_session, None)
                        continue
                    snapshot = await repos.snapshots.get(current.active_cycle_id)
                    if snapshot is not None:
                        active[candidate_session] = rehydrate_active_agent_cycle(snapshot)

            else:
                operation = "status"
                await diagnostics(repos).status(session_id)

            trace.append(f"{operation_index}:{session_id}:{operation}")
            current = await repos.sessions.get(session_id)
            trace_text = (
                f"seed={seed}; operation_index={operation_index}; operation={operation}; "
                f"session={session_id}; generation={current.generation if current else None}; "
                f"cycle={current.active_cycle_id if current else None}; "
                f"last_operations={list(trace)!r}"
            )
            await assert_invariants(repos, session_ids, trace=trace_text)
        except Exception as error:
            pytest.fail(
                f"IR-10 randomized failure: seed={seed}; operation_index={operation_index}; "
                f"operation={operation}; session={session_id}; "
                f"last_operations={list(trace)!r}; "
                f"error={type(error).__name__}: {error}",
                pytrace=True,
            )


@pytest.mark.asyncio
async def test_ir10_repeated_waiting_recovery_three_times_is_idempotent(tmp_path):
    initial = Batch("initial", "session", 1)
    reader = Reader(initial)
    repos, _, _, service = runtime(tmp_path, reader, cycle_prefix="repeat")
    admitted, _ = await seed_cycle(service, initial)
    cycle_id = admitted.target_cycle_id

    snapshot = await repos.snapshots.get(cycle_id)
    await repos.snapshots.compare_and_swap(
        snapshot.snapshot_revision,
        snapshot.model_copy(
            update={
                "status": CycleStatus.WAITING_USER,
                "waiting_question": "Persistent question?",
                "safe_checkpoint": CheckpointName.BEFORE_WAITING,
                "snapshot_revision": snapshot.snapshot_revision + 1,
                "updated_at": NOW,
            }
        ),
    )
    state = await repos.sessions.get("session")
    await repos.sessions.compare_and_swap(
        state.revision,
        state.model_copy(
            update={
                "cycle_status": CycleStatus.WAITING_USER,
                "revision": state.revision + 1,
                "updated_at": NOW,
            }
        ),
    )
    baseline_admissions = await repos.admissions.list_for_session("session")
    baseline_revisions = await repos.context_revisions.list_for_cycle(cycle_id)

    for repetition in range(3):
        fresh, _, _, _, plan = await recover(tmp_path, reader)
        recovered = next(item for item in plan.sessions if item.session_id == "session")
        assert recovered.disposition == RecoveryDisposition.WAITING, repetition
        assert recovered.cycle_id == cycle_id, repetition
        assert recovered.snapshot.waiting_question == "Persistent question?", repetition
        assert await fresh.admissions.list_for_session("session") == baseline_admissions
        assert await fresh.context_revisions.list_for_cycle(cycle_id) == baseline_revisions
        state = await fresh.sessions.get("session")
        assert state.active_cycle_id == cycle_id
        assert state.cycle_status == CycleStatus.WAITING_USER


def run_python(repo_root: Path, code: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code],
        cwd=repo_root,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_ir10_fresh_interpreter_persist_then_recover_has_no_inherited_memory(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    durable_root = str(tmp_path).replace("\\", "\\\\")
    process_a = f'''
import asyncio, json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from src.input_runtime import CheckpointName, CycleStatus, InputAdmissionService, InputRuntimeConfigType, create_filesystem_input_runtime_repositories
from src.runtime import ActiveAgentCycle, SessionExecutionCoordinator
from src.storage import StorageConfigType
NOW=datetime(2030,1,1,tzinfo=timezone.utc)
@dataclass
class Batch:
    input_batch_id:str='initial'; session_id:str='session'; sequence_number:int=1; payload_size:int=10
    text_parts:list=field(default_factory=list); artifact_refs:list=field(default_factory=list)
    source_event_ids:tuple=('evt_'+'1'*32,); content_fingerprint:str='sha256:'+'2'*64; committed_at:object=NOW
    continuation_of_batch_id:object=None; correction_of_batch_id:object=None
    artifact_manifest:object=field(default_factory=lambda:SimpleNamespace(items=()))
    def model_dump_json(self): return 'x'*self.payload_size
class Reader:
    def __init__(self): self.batch=Batch()
    async def get_committed(self, input_batch_id): return self.batch
    async def list_committed_for_recovery(self): return (self.batch,)
async def main():
    repos=create_filesystem_input_runtime_repositories(storage_config=StorageConfigType(root_dir=r'{durable_root}'))
    reader=Reader(); coordinator=SessionExecutionCoordinator()
    service=InputAdmissionService(config=InputRuntimeConfigType(), repositories=repos, committed_batches=reader, wake_coordinator=coordinator, cycle_id_factory=lambda:'process-cycle', clock=lambda:NOW, payload_size_resolver=lambda b:b.payload_size)
    outcome=await service.admit_committed_batch('initial', session_id='session')
    cycle=ActiveAgentCycle(cycle_id=outcome.target_cycle_id, session_id='session', original_user_request='initial', messages_for_llm=[{{'role':'system','content':'system'}},{{'role':'user','content':'{{"type":"user_request"}}'}}], cycle_trace=[], original_user_message_index=1, original_input_batch_id='initial', input_runtime_generation=0)
    await service.checkpoint_service.run_checkpoint(checkpoint=CheckpointName.RESUME, active_cycle=cycle, desired_status=CycleStatus.RUNNING)
    state=await repos.sessions.get('session')
    print(json.dumps({{'cycle':state.active_cycle_id,'generation':state.generation,'status':state.cycle_status.value}}))
asyncio.run(main())
'''
    first = run_python(repo_root, process_a)
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout.strip().splitlines()[-1]) == {
        "cycle": "process-cycle",
        "generation": 0,
        "status": "running",
    }

    process_b = f'''
import asyncio, json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from src.input_runtime import InputAdmissionService, InputRuntimeConfigType, create_filesystem_input_runtime_repositories
from src.input_runtime.recovery import InputRuntimeReadinessGate
from src.input_runtime.recovery_terminal import InputRuntimeRecoveryCoordinator
from src.runtime import SessionExecutionCoordinator
from src.storage import StorageConfigType
NOW=datetime(2030,1,1,tzinfo=timezone.utc)
@dataclass
class Batch:
    input_batch_id:str='initial'; session_id:str='session'; sequence_number:int=1; payload_size:int=10
    text_parts:list=field(default_factory=list); artifact_refs:list=field(default_factory=list)
    source_event_ids:tuple=('evt_'+'1'*32,); content_fingerprint:str='sha256:'+'2'*64; committed_at:object=NOW
    continuation_of_batch_id:object=None; correction_of_batch_id:object=None
    artifact_manifest:object=field(default_factory=lambda:SimpleNamespace(items=()))
    def model_dump_json(self): return 'x'*self.payload_size
class Reader:
    def __init__(self): self.batch=Batch()
    async def get_committed(self, input_batch_id): return self.batch
    async def list_committed_for_recovery(self): return (self.batch,)
async def main():
    repos=create_filesystem_input_runtime_repositories(storage_config=StorageConfigType(root_dir=r'{durable_root}'))
    reader=Reader(); coordinator=SessionExecutionCoordinator()
    service=InputAdmissionService(config=InputRuntimeConfigType(), repositories=repos, committed_batches=reader, wake_coordinator=coordinator, cycle_id_factory=lambda:'must-not-create', clock=lambda:NOW, payload_size_resolver=lambda b:b.payload_size)
    gate=InputRuntimeReadinessGate()
    recovery=InputRuntimeRecoveryCoordinator(repositories=repos, admission_service=service, committed_batches=reader, readiness_gate=gate, generation_coordinator=coordinator, clock=lambda:NOW)
    plan=await recovery.recover(); state=await repos.sessions.get('session'); admissions=await repos.admissions.list_for_session('session')
    print(json.dumps({{'cycle':state.active_cycle_id,'generation':state.generation,'status':state.cycle_status.value,'admissions':len(admissions),'disposition':plan.sessions[0].disposition.value}}))
asyncio.run(main())
'''
    second = run_python(repo_root, process_b)
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout.strip().splitlines()[-1]) == {
        "cycle": "process-cycle",
        "generation": 0,
        "status": "interrupted",
        "admissions": 1,
        "disposition": "auto_resume_safe",
    }
