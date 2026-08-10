from __future__ import annotations

import asyncio
import json
import os
import random
import subprocess
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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


NOW = datetime(2026, 8, 10, 7, 0, tzinfo=timezone.utc)
CI_SEEDS = (20260810, 104729, 8675309, 424242)
RELEASE_SEEDS = CI_SEEDS + (1, 7, 42, 271828, 314159, 1618033, 999983, 2147483647)
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


def make_repositories(root):
    return create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(root))
    )


def make_service(
    root,
    reader: Reader,
    *,
    config: InputRuntimeConfigType | None = None,
    repositories=None,
    cycle_prefix: str = "cycle",
):
    repositories = repositories or make_repositories(root)
    coordinator = SessionExecutionCoordinator()
    counter = iter(range(1, 100000))
    wake = Wake()
    service = InputAdmissionService(
        config=config or InputRuntimeConfigType(
            min_intermediate_message_interval_seconds=0
        ),
        repositories=repositories,
        committed_batches=reader,
        wake_coordinator=wake,
        cycle_id_factory=lambda: f"{cycle_prefix}-{next(counter)}",
        clock=lambda: NOW,
        payload_size_resolver=lambda batch: batch.payload_size,
    )
    return repositories, coordinator, wake, service


def make_cycle(*, session_id: str, cycle_id: str, input_batch_id: str, generation: int):
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


def diagnostics_for(repositories):
    return InputRuntimeDiagnosticsService(
        FileSystemRuntimeDiagnosticsReader(
            root=repositories.coordination_root,
            locks=repositories.coordination_locks,
        ),
        clock=lambda: NOW,
    )


async def start_cycle(service: InputAdmissionService, batch: Batch):
    outcome = await service.admit_committed_batch(
        batch.input_batch_id,
        session_id=batch.session_id,
    )
    assert outcome.action == InputAdmissionAction.START_CYCLE
    state = await service.repositories.sessions.get(batch.session_id)
    active = make_cycle(
        session_id=batch.session_id,
        cycle_id=outcome.target_cycle_id,
        input_batch_id=batch.input_batch_id,
        generation=state.generation,
    )
    initial = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.RESUME,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert initial.action in {
        CheckpointAction.CONTINUE,
        CheckpointAction.INPUT_APPLIED,
    }
    return outcome, active


async def recover(root, reader: Reader):
    repositories, coordinator, _, service = make_service(
        root,
        reader,
        cycle_prefix="recovery-must-not-allocate",
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
    plan = await recovery.recover()
    return repositories, coordinator, service, gate, plan


def _input_updates(active: ActiveAgentCycle) -> list[dict]:
    updates: list[dict] = []
    for message in active.messages_for_llm:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("type") == "input_batch_update":
            updates.append(payload)
    return updates


def _assert_tool_protocol(messages: list[dict]) -> None:
    pending: dict[str, int] = {}
    matched: dict[str, int] = defaultdict(int)
    for message in messages:
        if message.get("role") == "assistant":
            for tool_call in message.get("tool_calls") or ():
                call_id = tool_call.get("id")
                assert call_id and call_id not in pending
                pending[call_id] = 0
        elif message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            assert call_id in pending, f"orphan tool result: {call_id!r}"
            matched[call_id] += 1
            assert matched[call_id] == 1, f"duplicate tool result: {call_id!r}"
    assert set(pending) == set(matched), (
        f"open assistant tool block: calls={sorted(pending)} matched={sorted(matched)}"
    )


@pytest.mark.asyncio
async def test_ir10_synthetic_running_burst_is_one_cycle_fifo_and_read_only_status(tmp_path):
    batches = [
        Batch("initial", "session", 1, text_parts=[TextPart("p0", "message_text", "initial")]),
        Batch("a1", "session", 2, text_parts=[TextPart("p1", "message_text", "one")]),
        Batch("a2", "session", 3, text_parts=[TextPart("p2", "message_text", "two")]),
        Batch("a3", "session", 4, text_parts=[TextPart("p3", "message_text", "three")]),
    ]
    reader = Reader(*batches)
    repositories, _, wake, service = make_service(
        tmp_path,
        reader,
        config=InputRuntimeConfigType(
            max_queued_batches_per_session=8,
            max_queued_bytes_per_session=1000,
            max_batches_per_checkpoint=2,
            max_batch_bytes_per_checkpoint=1000,
            min_intermediate_message_interval_seconds=0,
        ),
        cycle_prefix="burst",
    )
    initial, active = await start_cycle(service, batches[0])
    cycle_id = initial.target_cycle_id

    for batch in batches[1:]:
        outcome = await service.admit_committed_batch(
            batch.input_batch_id,
            session_id="session",
        )
        assert outcome.action == InputAdmissionAction.QUEUED_RUNNING
        assert outcome.target_cycle_id == cycle_id
        assert outcome.should_start_runner is False

    before_status_state = await repositories.sessions.get("session")
    before_status_rows = await repositories.admissions.list_for_session("session")
    status = await diagnostics_for(repositories).status("session")
    after_status_state = await repositories.sessions.get("session")
    after_status_rows = await repositories.admissions.list_for_session("session")
    assert after_status_state == before_status_state
    assert after_status_rows == before_status_rows
    assert status.active_cycle_id == cycle_id
    assert status.input.queued == 3

    applied = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.BEFORE_LLM,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert applied.action == CheckpointAction.INPUT_APPLIED
    assert applied.applied_input_batch_ids == ("a1", "a2", "a3")
    assert applied.applied_through_cycle_sequence == 3

    admissions = await repositories.admissions.list_for_session("session")
    assert [row.session_sequence for row in admissions] == [1, 2, 3, 4]
    assert [row.cycle_sequence for row in admissions] == [0, 1, 2, 3]
    assert {row.target_cycle_id for row in admissions} == {cycle_id}
    assert [item.input_batch_id for item in await repositories.inbox.list_for_cycle(cycle_id)] == [
        "a1",
        "a2",
        "a3",
    ]
    assert len(_input_updates(active)) == 2
    assert all(call == ("session", cycle_id) for call in wake.calls)
    _assert_tool_protocol(active.messages_for_llm)


@pytest.mark.asyncio
async def test_ir10_synthetic_pause_resume_freezes_precontinue_input_and_leaves_late_input(tmp_path):
    batches = [
        Batch("initial", "session", 1),
        Batch("before", "session", 2, text_parts=[TextPart("b", "message_text", "before")]),
        Batch("after", "session", 3, text_parts=[TextPart("a", "message_text", "after")]),
    ]
    reader = Reader(*batches)
    repositories, _, _, service = make_service(
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
    initial, active = await start_cycle(service, batches[0])
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
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert paused.action == CheckpointAction.PAUSE
    assert (await repositories.sessions.get("session")).cycle_status == CycleStatus.PAUSED_BY_USER
    assert (await repositories.inbox.list_for_cycle(cycle_id))[0].state.value == "queued"

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
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert resumed.action == CheckpointAction.INPUT_APPLIED
    assert resumed.applied_input_batch_ids == ("before",)
    rows = await repositories.inbox.list_for_cycle(cycle_id)
    assert [(row.input_batch_id, row.state.value) for row in rows] == [
        ("before", "applied"),
        ("after", "queued"),
    ]

    ordinary = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.BEFORE_LLM,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert ordinary.applied_input_batch_ids == ("after",)
    state = await repositories.sessions.get("session")
    assert state.active_cycle_id == cycle_id
    assert state.active_cycle_accepted_through_sequence == 2
    assert state.active_cycle_applied_through_sequence == 2
    assert state.cycle_status == CycleStatus.RUNNING


@pytest.mark.asyncio
async def test_ir10_waiting_restart_reply_keeps_cycle_and_fifo(tmp_path):
    batches = [
        Batch("initial", "session", 1),
        Batch("queued", "session", 2, text_parts=[TextPart("q", "message_text", "queued")]),
        Batch("reply", "session", 3, text_parts=[TextPart("r", "message_text", "answer")]),
    ]
    reader = Reader(*batches)
    repositories, _, _, service = make_service(tmp_path, reader, cycle_prefix="waiting")
    initial, active = await start_cycle(service, batches[0])
    cycle_id = initial.target_cycle_id
    await service.admit_committed_batch("queued", session_id="session")

    snapshot = await repositories.snapshots.get(cycle_id)
    waiting_snapshot = snapshot.model_copy(
        update={
            "status": CycleStatus.WAITING_USER,
            "waiting_question": "Which option?",
            "safe_checkpoint": CheckpointName.BEFORE_WAITING,
            "snapshot_revision": snapshot.snapshot_revision + 1,
            "updated_at": NOW,
        }
    )
    await repositories.snapshots.compare_and_swap(
        snapshot.snapshot_revision,
        waiting_snapshot,
    )
    state = await repositories.sessions.get("session")
    waiting_state = state.model_copy(
        update={
            "cycle_status": CycleStatus.WAITING_USER,
            "revision": state.revision + 1,
            "updated_at": NOW,
        }
    )
    await repositories.sessions.compare_and_swap(state.revision, waiting_state)

    fresh, _, fresh_service, _, plan = await recover(tmp_path, reader)
    session_plan = next(item for item in plan.sessions if item.session_id == "session")
    assert session_plan.disposition == RecoveryDisposition.WAITING
    assert session_plan.cycle_id == cycle_id
    assert session_plan.snapshot.waiting_question == "Which option?"

    rehydrated = rehydrate_active_agent_cycle(session_plan.snapshot)
    reply = await fresh_service.admit_committed_batch("reply", session_id="session")
    assert reply.action == InputAdmissionAction.RESUME_WAITING
    assert reply.target_cycle_id == cycle_id
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
    assert len(await fresh.context_revisions.list_for_cycle(cycle_id)) == 2


@pytest.mark.asyncio
async def test_ir10_reset_crash_restart_fences_old_generation_and_starts_new_cycle(tmp_path, monkeypatch):
    batches = [
        Batch("initial", "session", 1),
        Batch("old-addition", "session", 2),
        Batch("new-input", "session", 3),
    ]
    reader = Reader(*batches)
    repositories, _, _, service = make_service(tmp_path, reader, cycle_prefix="reset")
    initial, _ = await start_cycle(service, batches[0])
    old_cycle = initial.target_cycle_id
    await service.admit_committed_batch("old-addition", session_id="session")

    real_cancel = repositories.snapshots.cancel_generation

    async def crash_cleanup(*args, **kwargs):
        raise OSError("ir10 reset cleanup crash")

    monkeypatch.setattr(repositories.snapshots, "cancel_generation", crash_cleanup)
    with pytest.raises(OSError, match="ir10 reset cleanup crash"):
        await service.control_service.request_reset(
            session_id="session",
            idempotency_key="ir10-reset",
            source_client_type="test",
        )
    monkeypatch.setattr(repositories.snapshots, "cancel_generation", real_cancel)

    crashed = await repositories.sessions.get("session")
    assert crashed.generation == 1
    assert crashed.active_cycle_id is None

    fresh, _, fresh_service, _, plan = await recover(tmp_path, reader)
    assert plan.sessions == ()
    state = await fresh.sessions.get("session")
    assert state.generation == 1
    assert state.cycle_status == CycleStatus.IDLE
    old_snapshot = await fresh.snapshots.get(old_cycle)
    assert old_snapshot.status == CycleStatus.CANCELLED

    fresh_service.cycle_id_factory = lambda: "reset-new-cycle"
    new = await fresh_service.admit_committed_batch("new-input", session_id="session")
    assert new.action == InputAdmissionAction.START_CYCLE
    assert new.target_cycle_id == "reset-new-cycle"
    assert new.admission.admitted_generation == 1
    assert new.target_cycle_id != old_cycle

    old_rows = await fresh.admissions.list_for_session("session")
    old_addition = next(row for row in old_rows if row.input_batch_id == "old-addition")
    assert old_addition.state.value == "cancelled"
    current = await fresh.sessions.get("session")
    assert current.generation == 1
    assert current.active_cycle_id == "reset-new-cycle"


@pytest.mark.asyncio
async def test_ir10_capacity_exact_limits_duplicate_and_fifo_head_are_not_bypassed(tmp_path):
    batches = [
        Batch("initial", "session", 1, payload_size=1),
        Batch("exact-a", "session", 2, payload_size=5),
        Batch("exact-b", "session", 3, payload_size=5),
        Batch("plus-one", "session", 4, payload_size=1),
    ]
    config = InputRuntimeConfigType(
        max_queued_batches_per_session=2,
        max_queued_bytes_per_session=10,
        max_batches_per_checkpoint=1,
        max_batch_bytes_per_checkpoint=5,
        min_intermediate_message_interval_seconds=0,
    )
    reader = Reader(*batches)
    repositories, _, _, service = make_service(
        tmp_path,
        reader,
        config=config,
        cycle_prefix="capacity",
    )
    initial, active = await start_cycle(service, batches[0])
    cycle_id = initial.target_cycle_id
    one = await service.admit_committed_batch("exact-a", session_id="session")
    two = await service.admit_committed_batch("exact-b", session_id="session")
    assert one.action == two.action == InputAdmissionAction.QUEUED_RUNNING

    duplicate = await service.admit_committed_batch("exact-a", session_id="session")
    assert duplicate.action == InputAdmissionAction.DUPLICATE
    blocked = await service.admit_committed_batch("plus-one", session_id="session")
    assert blocked.action == InputAdmissionAction.CAPACITY_BLOCKED
    assert blocked.reason_code in {
        "max_queued_batches_per_session",
        "max_queued_bytes_per_session",
    }
    assert await repositories.admissions.get_by_input_batch_id("plus-one") is None

    first = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.BEFORE_LLM,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert first.applied_input_batch_ids == ("exact-a",)
    second = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.BEFORE_LLM,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert second.applied_input_batch_ids == ("exact-b",)
    assert [row.cycle_sequence for row in await repositories.inbox.list_for_cycle(cycle_id)] == [1, 2]


async def _assert_global_invariants(repositories, sessions: tuple[str, ...], *, trace: str) -> None:
    all_admission_ids: set[str] = set()
    all_input_ids: set[str] = set()
    current_cycles: dict[str, str] = {}

    for session_id in sessions:
        state = await repositories.sessions.get(session_id)
        assert state is not None, trace
        assert state.active_cycle_applied_through_sequence <= state.active_cycle_accepted_through_sequence, trace
        assert state.applied_control_sequence <= state.pending_control_sequence, trace

        rows = await repositories.admissions.list_for_session(session_id)
        session_sequences = [row.session_sequence for row in rows]
        if session_sequences:
            assert session_sequences == list(range(1, len(session_sequences) + 1)), trace
        assert len({row.input_batch_id for row in rows}) == len(rows), trace
        assert len({row.admission_id for row in rows}) == len(rows), trace
        for row in rows:
            assert row.session_id == session_id, trace
            assert row.admission_id not in all_admission_ids, trace
            assert row.input_batch_id not in all_input_ids, trace
            all_admission_ids.add(row.admission_id)
            all_input_ids.add(row.input_batch_id)

        by_cycle: dict[tuple[int, str], list[object]] = defaultdict(list)
        for row in rows:
            by_cycle[(row.admitted_generation, row.target_cycle_id)].append(row)
        for key, cycle_rows in by_cycle.items():
            seqs = sorted(row.cycle_sequence for row in cycle_rows)
            assert seqs == list(range(0, len(seqs))), f"{trace}; cycle={key}; seqs={seqs}"

        controls = await repositories.controls.list_for_session(session_id)
        control_sequences = [row.sequence_number for row in controls]
        if control_sequences:
            assert control_sequences == list(range(1, len(control_sequences) + 1)), trace
            keys = [row.idempotency_key for row in controls]
            assert len(keys) == len(set(keys)), trace

        if state.active_cycle_id is not None:
            current_cycles[session_id] = state.active_cycle_id
            snapshot = await repositories.snapshots.get(state.active_cycle_id)
            if snapshot is not None:
                assert snapshot.session_id == session_id, trace
                assert snapshot.generation == state.generation, trace
                assert snapshot.applied_through_cycle_sequence <= state.active_cycle_accepted_through_sequence, trace
                _assert_tool_protocol(snapshot.messages_for_llm)

        before = state
        status = await diagnostics_for(repositories).status(session_id)
        after = await repositories.sessions.get(session_id)
        assert after == before, f"{trace}; diagnostics mutated session"
        assert status.generation == state.generation, trace
        assert status.active_cycle_id == state.active_cycle_id, trace

    assert len(set(current_cycles.values())) == len(current_cycles), trace


@pytest.mark.asyncio
@pytest.mark.parametrize("seed", CI_SEEDS)
async def test_ir10_seeded_multisession_state_machine(seed, tmp_path):
    rng = random.Random(seed)
    sessions = ("session-a", "session-b", "session-c")
    batches: list[Batch] = []
    batch_queues: dict[str, deque[Batch]] = {}
    for session_index, session_id in enumerate(sessions):
        items = [
            Batch(
                f"{session_id}-batch-{index}",
                session_id,
                index + 1,
                payload_size=1 + ((seed + index + session_index) % 9),
                text_parts=[
                    TextPart(
                        f"p-{session_index}-{index}",
                        "message_text",
                        f"synthetic-{session_index}-{index}",
                    )
                ],
            )
            for index in range(70)
        ]
        batches.extend(items)
        batch_queues[session_id] = deque(items)

    reader = Reader(*batches)
    config = InputRuntimeConfigType(
        max_queued_batches_per_session=8,
        max_queued_bytes_per_session=64,
        max_batches_per_checkpoint=3,
        max_batch_bytes_per_checkpoint=24,
        min_intermediate_message_interval_seconds=0,
    )
    repositories, _, _, service = make_service(
        tmp_path,
        reader,
        config=config,
        cycle_prefix=f"seed-{seed}",
    )
    active: dict[str, ActiveAgentCycle] = {}
    admitted_ids: dict[str, list[str]] = defaultdict(list)
    trace: deque[str] = deque(maxlen=TRACE_LIMIT)

    for session_id in sessions:
        first = batch_queues[session_id].popleft()
        outcome, cycle = await start_cycle(service, first)
        active[session_id] = cycle
        admitted_ids[session_id].append(first.input_batch_id)
        trace.append(f"bootstrap:{session_id}:{outcome.target_cycle_id}")

    steps = int(os.environ.get("IR10_RANDOM_STEPS", "180"))
    for op_index in range(steps):
        session_id = rng.choice(sessions)
        state = await repositories.sessions.get(session_id)
        choice = rng.randrange(100)
        op = "status"

        try:
            if choice < 36 and batch_queues[session_id]:
                batch = batch_queues[session_id].popleft()
                op = f"admit:{batch.input_batch_id}"
                outcome = await service.admit_committed_batch(
                    batch.input_batch_id,
                    session_id=session_id,
                )
                if outcome.action != InputAdmissionAction.CAPACITY_BLOCKED:
                    admitted_ids[session_id].append(batch.input_batch_id)
                    if outcome.action == InputAdmissionAction.START_CYCLE:
                        active[session_id] = make_cycle(
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
                else:
                    batch_queues[session_id].appendleft(batch)

            elif choice < 46 and admitted_ids[session_id]:
                batch_id = rng.choice(admitted_ids[session_id])
                op = f"duplicate:{batch_id}"
                duplicate = await service.admit_committed_batch(
                    batch_id,
                    session_id=session_id,
                )
                assert duplicate.action == InputAdmissionAction.DUPLICATE

            elif choice < 62 and state.active_cycle_id is not None and session_id in active:
                if state.cycle_status in {CycleStatus.RUNNING, CycleStatus.PAUSE_REQUESTED}:
                    op = "checkpoint"
                    await service.checkpoint_service.run_checkpoint(
                        checkpoint=CheckpointName.BEFORE_LLM,
                        active_cycle=active[session_id],
                        desired_status=CycleStatus.RUNNING,
                    )

            elif choice < 70 and state.cycle_status == CycleStatus.RUNNING:
                op = "pause"
                await service.control_service.request_pause(
                    session_id=session_id,
                    idempotency_key=f"{seed}:{op_index}:pause",
                    source_client_type="ir10",
                )

            elif choice < 77 and state.cycle_status in {
                CycleStatus.PAUSED_BY_USER,
                CycleStatus.PAUSE_REQUESTED,
            }:
                op = "continue"
                await service.control_service.request_continue(
                    session_id=session_id,
                    idempotency_key=f"{seed}:{op_index}:continue",
                    source_client_type="ir10",
                )
                if session_id in active:
                    await service.checkpoint_service.run_checkpoint(
                        checkpoint=CheckpointName.RESUME,
                        active_cycle=active[session_id],
                        desired_status=CycleStatus.RUNNING,
                    )

            elif choice < 84 and state.active_cycle_id is not None:
                op = "reset"
                await service.control_service.request_reset(
                    session_id=session_id,
                    idempotency_key=f"{seed}:{op_index}:reset",
                    source_client_type="ir10",
                )
                active.pop(session_id, None)

            elif choice < 92:
                op = "repository-reopen"
                repositories, _, _, service = make_service(
                    tmp_path,
                    reader,
                    config=config,
                    cycle_prefix=f"seed-{seed}-reopen-{op_index}",
                )
                for sid in sessions:
                    current = await repositories.sessions.get(sid)
                    if current is None or current.active_cycle_id is None:
                        active.pop(sid, None)
                        continue
                    snapshot = await repositories.snapshots.get(current.active_cycle_id)
                    if snapshot is not None:
                        active[sid] = rehydrate_active_agent_cycle(snapshot)

            else:
                op = "status"
                await diagnostics_for(repositories).status(session_id)

            trace.append(f"{op_index}:{session_id}:{op}")
            state_after = await repositories.sessions.get(session_id)
            trace_text = (
                f"seed={seed}; operation_index={op_index}; operation={op}; "
                f"session={session_id}; generation={state_after.generation if state_after else None}; "
                f"cycle={state_after.active_cycle_id if state_after else None}; "
                f"last_operations={list(trace)!r}"
            )
            await _assert_global_invariants(repositories, sessions, trace=trace_text)
        except Exception as error:
            pytest.fail(
                f"IR-10 randomized failure: seed={seed}; operation_index={op_index}; "
                f"operation={op}; session={session_id}; last_operations={list(trace)!r}; "
                f"error={type(error).__name__}: {error}",
                pytrace=True,
            )


@pytest.mark.asyncio
async def test_ir10_repeated_waiting_recovery_is_idempotent(tmp_path):
    batch = Batch("initial", "session", 1)
    reader = Reader(batch)
    repositories, _, _, service = make_service(tmp_path, reader, cycle_prefix="repeat")
    initial, _ = await start_cycle(service, batch)
    cycle_id = initial.target_cycle_id

    snapshot = await repositories.snapshots.get(cycle_id)
    waiting_snapshot = snapshot.model_copy(
        update={
            "status": CycleStatus.WAITING_USER,
            "waiting_question": "Persistent question?",
            "safe_checkpoint": CheckpointName.BEFORE_WAITING,
            "snapshot_revision": snapshot.snapshot_revision + 1,
            "updated_at": NOW,
        }
    )
    await repositories.snapshots.compare_and_swap(snapshot.snapshot_revision, waiting_snapshot)
    state = await repositories.sessions.get("session")
    await repositories.sessions.compare_and_swap(
        state.revision,
        state.model_copy(
            update={
                "cycle_status": CycleStatus.WAITING_USER,
                "revision": state.revision + 1,
                "updated_at": NOW,
            }
        ),
    )

    baseline_admissions = await repositories.admissions.list_for_session("session")
    baseline_revisions = await repositories.context_revisions.list_for_cycle(cycle_id)
    for repetition in range(3):
        fresh, _, _, _, plan = await recover(tmp_path, reader)
        session_plan = next(item for item in plan.sessions if item.session_id == "session")
        assert session_plan.disposition == RecoveryDisposition.WAITING, repetition
        assert session_plan.cycle_id == cycle_id, repetition
        assert session_plan.snapshot.waiting_question == "Persistent question?", repetition
        assert await fresh.admissions.list_for_session("session") == baseline_admissions
        assert await fresh.context_revisions.list_for_cycle(cycle_id) == baseline_revisions
        durable = await fresh.sessions.get("session")
        assert durable.active_cycle_id == cycle_id
        assert durable.cycle_status == CycleStatus.WAITING_USER


def _run_python(root: Path, code: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_ir10_fresh_interpreter_persist_then_recover(tmp_path):
    repo_root = Path(__file__).resolve().parents[1]
    durable_root = str(tmp_path).replace("\\", "\\\\")
    process_a = f"""
import asyncio, json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from src.input_runtime import CheckpointName, CycleStatus, InputAdmissionService, InputRuntimeConfigType, create_filesystem_input_runtime_repositories
from src.runtime import ActiveAgentCycle, SessionExecutionCoordinator
from src.storage import StorageConfigType

NOW = datetime(2026,8,10,7,0,tzinfo=timezone.utc)
@dataclass
class Batch:
    input_batch_id: str = 'initial'
    session_id: str = 'session'
    sequence_number: int = 1
    payload_size: int = 10
    text_parts: list = field(default_factory=list)
    artifact_refs: list = field(default_factory=list)
    source_event_ids: tuple = ('evt_' + '1'*32,)
    content_fingerprint: str = 'sha256:' + '2'*64
    committed_at: object = NOW
    continuation_of_batch_id: object = None
    correction_of_batch_id: object = None
    artifact_manifest: object = field(default_factory=lambda: SimpleNamespace(items=()))
    def model_dump_json(self): return 'x'*self.payload_size
class Reader:
    def __init__(self): self.batch = Batch()
    async def get_committed(self, input_batch_id): return self.batch
    async def list_committed_for_recovery(self): return (self.batch,)
async def main():
    repos = create_filesystem_input_runtime_repositories(storage_config=StorageConfigType(root_dir=r'{durable_root}'))
    reader = Reader()
    service = InputAdmissionService(config=InputRuntimeConfigType(), repositories=repos, committed_batches=reader, wake_coordinator=SessionExecutionCoordinator(), cycle_id_factory=lambda:'process-cycle', clock=lambda:NOW, payload_size_resolver=lambda batch:batch.payload_size)
    outcome = await service.admit_committed_batch('initial', session_id='session')
    active = ActiveAgentCycle(cycle_id=outcome.target_cycle_id, session_id='session', original_user_request='initial', messages_for_llm=[{{'role':'system','content':'system'}},{{'role':'user','content':'{{\"type\":\"user_request\"}}'}}], cycle_trace=[], original_user_message_index=1, original_input_batch_id='initial', input_runtime_generation=0)
    await service.checkpoint_service.run_checkpoint(checkpoint=CheckpointName.RESUME, active_cycle=active, desired_status=CycleStatus.RUNNING)
    state = await repos.sessions.get('session')
    print(json.dumps({{'cycle':state.active_cycle_id,'generation':state.generation,'status':state.cycle_status.value}}))
asyncio.run(main())
"""
    first = _run_python(repo_root, process_a)
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout.strip().splitlines()[-1]) == {
        "cycle": "process-cycle",
        "generation": 0,
        "status": "running",
    }

    process_b = f"""
import asyncio, json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from src.input_runtime import InputAdmissionService, InputRuntimeConfigType, create_filesystem_input_runtime_repositories
from src.input_runtime.recovery import InputRuntimeReadinessGate
from src.input_runtime.recovery_terminal import InputRuntimeRecoveryCoordinator
from src.runtime import SessionExecutionCoordinator
from src.storage import StorageConfigType

NOW = datetime(2026,8,10,7,0,tzinfo=timezone.utc)
@dataclass
class Batch:
    input_batch_id: str = 'initial'
    session_id: str = 'session'
    sequence_number: int = 1
    payload_size: int = 10
    text_parts: list = field(default_factory=list)
    artifact_refs: list = field(default_factory=list)
    source_event_ids: tuple = ('evt_' + '1'*32,)
    content_fingerprint: str = 'sha256:' + '2'*64
    committed_at: object = NOW
    continuation_of_batch_id: object = None
    correction_of_batch_id: object = None
    artifact_manifest: object = field(default_factory=lambda: SimpleNamespace(items=()))
    def model_dump_json(self): return 'x'*self.payload_size
class Reader:
    def __init__(self): self.batch = Batch()
    async def get_committed(self, input_batch_id): return self.batch
    async def list_committed_for_recovery(self): return (self.batch,)
async def main():
    repos = create_filesystem_input_runtime_repositories(storage_config=StorageConfigType(root_dir=r'{durable_root}'))
    reader = Reader()
    coordinator = SessionExecutionCoordinator()
    service = InputAdmissionService(config=InputRuntimeConfigType(), repositories=repos, committed_batches=reader, wake_coordinator=coordinator, cycle_id_factory=lambda:'must-not-create', clock=lambda:NOW, payload_size_resolver=lambda batch:batch.payload_size)
    gate = InputRuntimeReadinessGate()
    recovery = InputRuntimeRecoveryCoordinator(repositories=repos, admission_service=service, committed_batches=reader, readiness_gate=gate, generation_coordinator=coordinator, clock=lambda:NOW)
    plan = await recovery.recover()
    state = await repos.sessions.get('session')
    admissions = await repos.admissions.list_for_session('session')
    print(json.dumps({{'cycle':state.active_cycle_id,'generation':state.generation,'status':state.cycle_status.value,'admissions':len(admissions),'disposition':plan.sessions[0].disposition.value}}))
asyncio.run(main())
"""
    second = _run_python(repo_root, process_b)
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout.strip().splitlines()[-1]) == {
        "cycle": "process-cycle",
        "generation": 0,
        "status": "interrupted",
        "admissions": 1,
        "disposition": "auto_resume_safe",
    }
