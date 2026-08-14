from __future__ import annotations

import pytest

from src.input_runtime import (
    CheckpointAction,
    CheckpointName,
    CycleStatus,
    InputAdmissionAction,
    InputRuntimeConfigType,
)
from src.input_runtime.recovery import (
    InputRuntimeLifecycleState,
    InputRuntimeReadinessGate,
    RecoveryDisposition,
)
from src.input_runtime.recovery_backpressure import InputRuntimeRecoveryCoordinator
from tests.test_input_runtime_ir10_release import (
    NOW,
    Batch,
    Reader,
    active_cycle,
    runtime,
)


def _config(*, queue_limit: int) -> InputRuntimeConfigType:
    return InputRuntimeConfigType(
        max_queued_batches_per_session=queue_limit,
        max_queued_bytes_per_session=1024 * 1024,
        max_batches_per_checkpoint=queue_limit,
        max_batch_bytes_per_checkpoint=1024 * 1024,
        min_intermediate_message_interval_seconds=0,
    )


async def _recover(root, reader: Reader, *, queue_limit: int, cycle_prefix: str):
    repos, coordinator, _, service = runtime(
        root,
        reader,
        config=_config(queue_limit=queue_limit),
        cycle_prefix=cycle_prefix,
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
    return repos, service, gate, plan


@pytest.mark.asyncio
async def test_recovery_defers_capacity_overflow_and_drains_all_131_in_fifo(tmp_path):
    batches = [
        Batch(f"batch-{sequence:03d}", "session", sequence, payload_size=1)
        for sequence in range(1, 132)
    ]
    reader = Reader(*batches)

    repos, service, gate, plan = await _recover(
        tmp_path,
        reader,
        queue_limit=64,
        cycle_prefix="recovery",
    )

    rows = await repos.admissions.list_for_session("session")
    assert len(rows) == 65  # initial cycle batch + 64 bounded additions
    assert service.recovery_deferred_count("session") == 66
    assert service.recovery_deferred_ids("session")[0] == "batch-066"
    assert service.recovery_deferred_ids("session")[-1] == "batch-131"
    assert gate.state == InputRuntimeLifecycleState.RECOVERING
    assert len(plan.sessions) == 1
    assert plan.sessions[0].disposition == RecoveryDisposition.START_ADMITTED

    # A second process must reconstruct the same backlog from durable authority,
    # not from the first process's in-memory deferred queue.
    repos, service, gate, replay = await _recover(
        tmp_path,
        reader,
        queue_limit=64,
        cycle_prefix="must-not-allocate",
    )
    rows = await repos.admissions.list_for_session("session")
    assert len(rows) == 65
    assert service.recovery_deferred_count("session") == 66
    assert gate.state == InputRuntimeLifecycleState.RECOVERING
    assert replay.sessions[0].disposition == RecoveryDisposition.START_ADMITTED

    state = await repos.sessions.get("session")
    assert state is not None
    cycle = active_cycle(
        session_id="session",
        cycle_id=state.active_cycle_id,
        input_batch_id="batch-001",
        generation=state.generation,
    )

    outcomes = []
    for checkpoint in (
        CheckpointName.RESUME,
        CheckpointName.BEFORE_LLM,
        CheckpointName.AFTER_TOOL_BLOCK,
    ):
        outcomes.append(
            await service.checkpoint_service.run_checkpoint(
                checkpoint=checkpoint,
                active_cycle=cycle,
                desired_status=CycleStatus.RUNNING,
            )
        )

    assert all(
        outcome.action == CheckpointAction.INPUT_APPLIED
        for outcome in outcomes
    )
    assert service.recovery_deferred_count("session") == 0

    rows = await repos.admissions.list_for_session("session")
    assert len(rows) == 131
    assert [row.input_batch_id for row in rows] == [
        batch.input_batch_id for batch in batches
    ]
    assert [row.session_sequence for row in rows] == list(range(1, 132))
    assert [row.cycle_sequence for row in rows] == list(range(0, 131))

    state = await repos.sessions.get("session")
    assert state is not None
    assert state.active_cycle_accepted_through_sequence == 130
    assert state.active_cycle_applied_through_sequence == 130


@pytest.mark.asyncio
async def test_live_input_cannot_overtake_recovery_deferred_fifo(tmp_path):
    initial = Batch("initial", "session", 1, payload_size=1)
    old = [
        Batch(f"old-{sequence}", "session", sequence + 1, payload_size=1)
        for sequence in range(1, 5)
    ]
    reader = Reader(initial, *old)

    repos, service, _, plan = await _recover(
        tmp_path,
        reader,
        queue_limit=2,
        cycle_prefix="priority",
    )
    assert service.recovery_deferred_ids("session") == ("old-3", "old-4")

    live = Batch("live", "session", 6, payload_size=1)
    reader.add(live)
    blocked = await service.admit_committed_batch(
        live.input_batch_id,
        session_id="session",
    )
    assert blocked.action == InputAdmissionAction.CAPACITY_BLOCKED
    assert blocked.reason_code == "recovery_deferred_backlog"
    assert await repos.admissions.get_by_input_batch_id("live") is None

    state = await repos.sessions.get("session")
    assert state is not None
    cycle = active_cycle(
        session_id="session",
        cycle_id=plan.sessions[0].cycle_id,
        input_batch_id="initial",
        generation=state.generation,
    )

    first = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.RESUME,
        active_cycle=cycle,
        desired_status=CycleStatus.RUNNING,
    )
    assert first.action == CheckpointAction.INPUT_APPLIED
    assert service.recovery_deferred_count("session") == 0

    rows = await repos.admissions.list_for_session("session")
    assert [row.input_batch_id for row in rows] == [
        "initial",
        "old-1",
        "old-2",
        "old-3",
        "old-4",
    ]

    still_blocked = await service.admit_committed_batch(
        "live",
        session_id="session",
    )
    assert still_blocked.action == InputAdmissionAction.CAPACITY_BLOCKED
    assert still_blocked.reason_code == "max_queued_batches_per_session"

    second = await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.BEFORE_LLM,
        active_cycle=cycle,
        desired_status=CycleStatus.RUNNING,
    )
    assert second.action == CheckpointAction.INPUT_APPLIED

    admitted = await service.admit_committed_batch(
        "live",
        session_id="session",
    )
    assert admitted.action == InputAdmissionAction.QUEUED_RUNNING

    rows = await repos.admissions.list_for_session("session")
    assert [row.input_batch_id for row in rows] == [
        "initial",
        "old-1",
        "old-2",
        "old-3",
        "old-4",
        "live",
    ]