from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from src.input_runtime import InputAdmissionAction, InputRuntimeConfigType
from src.input_runtime.models import AdmissionState, CycleStatus
from src.input_runtime.recovery import (
    InputRuntimeLifecycleState,
    InputRuntimeReadinessGate,
    RecoveryDisposition,
)
from src.input_runtime.recovery_backpressure import InputRuntimeRecoveryCoordinator
from tests.test_input_runtime_ir10_release import NOW, Batch, Reader, runtime, seed_cycle


TTL_SECONDS = 6 * 60 * 60


def _config(*, ttl_seconds: int = TTL_SECONDS) -> InputRuntimeConfigType:
    return InputRuntimeConfigType(
        max_queued_batches_per_session=64,
        max_queued_bytes_per_session=1024 * 1024,
        max_batches_per_checkpoint=8,
        max_batch_bytes_per_checkpoint=1024 * 1024,
        recovery_auto_resume_max_age_seconds=ttl_seconds,
        min_intermediate_message_interval_seconds=0,
    )


async def _recover(root, reader: Reader, *, config: InputRuntimeConfigType):
    repos, coordinator, _, service = runtime(
        root,
        reader,
        config=config,
        cycle_prefix="recovery-lifecycle",
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


def test_recovery_auto_resume_age_config_is_explicit_and_non_negative():
    assert InputRuntimeConfigType().recovery_auto_resume_max_age_seconds == TTL_SECONDS
    assert InputRuntimeConfigType(
        recovery_auto_resume_max_age_seconds=0
    ).recovery_auto_resume_max_age_seconds == 0
    with pytest.raises(ValidationError):
        InputRuntimeConfigType(recovery_auto_resume_max_age_seconds=-1)


@pytest.mark.asyncio
async def test_stale_committed_unadmitted_batch_does_not_start_new_cycle(tmp_path):
    old = Batch(
        "stale-unadmitted",
        "session",
        1,
        committed_at=NOW - timedelta(hours=7),
    )
    reader = Reader(old)

    repos, service, gate, plan = await _recover(
        tmp_path,
        reader,
        config=_config(),
    )

    assert plan.sessions == ()
    assert await repos.admissions.get_by_input_batch_id(old.input_batch_id) is None
    assert await repos.sessions.get(old.session_id) is None
    assert service.recovery_deferred_count(old.session_id) == 0
    assert gate.state == InputRuntimeLifecycleState.RECOVERING


@pytest.mark.asyncio
async def test_fresh_start_admitted_remains_auto_schedulable(tmp_path):
    fresh = Batch(
        "fresh-start",
        "session",
        1,
        committed_at=NOW - timedelta(minutes=5),
    )
    reader = Reader(fresh)

    _, _, _, plan = await _recover(tmp_path, reader, config=_config())

    assert len(plan.sessions) == 1
    assert plan.sessions[0].disposition == RecoveryDisposition.START_ADMITTED
    assert plan.sessions[0].should_auto_schedule is True


@pytest.mark.asyncio
async def test_exact_recovery_ttl_boundary_is_still_eligible(tmp_path):
    boundary = Batch(
        "ttl-boundary",
        "session",
        1,
        committed_at=NOW - timedelta(seconds=TTL_SECONDS),
    )
    reader = Reader(boundary)

    _, _, _, plan = await _recover(tmp_path, reader, config=_config())

    assert len(plan.sessions) == 1
    assert plan.sessions[0].disposition == RecoveryDisposition.START_ADMITTED
    assert plan.sessions[0].should_auto_schedule is True


@pytest.mark.asyncio
async def test_stale_start_admitted_is_durably_reset_without_runner(tmp_path):
    old = Batch(
        "stale-start",
        "session",
        1,
        committed_at=NOW - timedelta(hours=7),
    )
    reader = Reader(old)
    repos, _, _, service = runtime(
        tmp_path,
        reader,
        config=_config(),
        cycle_prefix="stale-start",
    )
    admitted = await service.admit_committed_batch(
        old.input_batch_id,
        session_id=old.session_id,
    )
    assert admitted.action == InputAdmissionAction.START_CYCLE

    repos, service, _, plan = await _recover(
        tmp_path,
        reader,
        config=_config(),
    )

    assert plan.sessions == ()
    state = await repos.sessions.get(old.session_id)
    assert state is not None
    assert state.cycle_status == CycleStatus.IDLE
    assert state.active_cycle_id is None
    assert state.generation == 1
    admission = await repos.admissions.get_by_input_batch_id(old.input_batch_id)
    assert admission is not None
    assert admission.state == AdmissionState.CANCELLED
    assert service.recovery_deferred_count(old.session_id) == 0


@pytest.mark.asyncio
async def test_stale_running_cycle_becomes_manual_interrupted_even_if_snapshot_is_new(tmp_path):
    old = Batch(
        "stale-running",
        "session",
        1,
        committed_at=NOW - timedelta(hours=7),
    )
    reader = Reader(old)
    repos, _, _, service = runtime(
        tmp_path,
        reader,
        config=_config(),
        cycle_prefix="stale-running",
    )
    admitted, _ = await seed_cycle(service, old)

    # seed_cycle wrote the runtime snapshot at NOW. The immutable input is still
    # seven hours old, so a recovery write cannot renew automatic execution.
    snapshot_before = await repos.snapshots.get(admitted.target_cycle_id)
    assert snapshot_before is not None
    assert snapshot_before.updated_at == NOW

    repos, _, _, plan = await _recover(tmp_path, reader, config=_config())

    assert len(plan.sessions) == 1
    recovered = plan.sessions[0]
    assert recovered.disposition == RecoveryDisposition.INTERRUPTED
    assert recovered.reason_code == "recovery_window_expired"
    assert recovered.should_auto_schedule is False
    state = await repos.sessions.get(old.session_id)
    assert state is not None
    assert state.cycle_status == CycleStatus.INTERRUPTED
    snapshot = await repos.snapshots.get(recovered.cycle_id)
    assert snapshot is not None
    assert snapshot.status == CycleStatus.INTERRUPTED
    assert snapshot.interruption_reason == "recovery_window_expired"


@pytest.mark.asyncio
async def test_fresh_running_cycle_keeps_safe_auto_resume(tmp_path):
    fresh = Batch(
        "fresh-running",
        "session",
        1,
        committed_at=NOW - timedelta(hours=1),
    )
    reader = Reader(fresh)
    _, _, _, service = runtime(
        tmp_path,
        reader,
        config=_config(),
        cycle_prefix="fresh-running",
    )
    await seed_cycle(service, fresh)

    _, _, _, plan = await _recover(tmp_path, reader, config=_config())

    assert len(plan.sessions) == 1
    assert plan.sessions[0].disposition == RecoveryDisposition.AUTO_RESUME_SAFE
    assert plan.sessions[0].reason_code == "startup_safe_restart"
    assert plan.sessions[0].should_auto_schedule is True
