from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.api.session_reset import reset_runtime_session
from src.input_runtime.recovery import (
    InputRuntimeLifecycleState,
    InputRuntimeReadinessGate,
    InputRuntimeRecoveryCoordinator,
    InputRuntimeRecoveryError,
    InputRuntimeRecoveryPlan,
)

NOW = datetime(2026, 8, 8, 21, 0, tzinfo=timezone.utc)


@dataclass
class _BlockingRecovery(InputRuntimeRecoveryCoordinator):
    entered: asyncio.Event
    release: asyncio.Event
    readiness_gate: InputRuntimeReadinessGate

    def __init__(self, *, entered, release, readiness_gate):
        self.entered = entered
        self.release = release
        self.readiness_gate = readiness_gate

    async def _recover(self, report):
        self.entered.set()
        await self.release.wait()
        return InputRuntimeRecoveryPlan(sessions=(), report=report)


@pytest.mark.asyncio
async def test_gate_is_recovering_while_reconciliation_is_blocked():
    gate = InputRuntimeReadinessGate()
    entered = asyncio.Event()
    release = asyncio.Event()
    recovery = _BlockingRecovery(
        entered=entered,
        release=release,
        readiness_gate=gate,
    )

    task = asyncio.create_task(recovery.recover())
    await entered.wait()
    assert gate.state == InputRuntimeLifecycleState.RECOVERING
    assert gate.is_ready is False
    with pytest.raises(InputRuntimeRecoveryError) as error:
        gate.require_ready()
    assert error.value.reason_code == "input_runtime_not_ready"

    release.set()
    plan = await task
    assert plan.sessions == ()
    assert gate.state == InputRuntimeLifecycleState.RECOVERING


@pytest.mark.asyncio
async def test_runner_waits_for_ready_without_polling_or_sleep():
    gate = InputRuntimeReadinessGate()
    gate.begin_recovery()
    entered = asyncio.Event()

    async def runner():
        await gate.wait_ready()
        entered.set()

    task = asyncio.create_task(runner())
    assert entered.is_set() is False
    gate.mark_ready()
    await entered.wait()
    await task
    assert gate.state == InputRuntimeLifecycleState.READY


@pytest.mark.asyncio
async def test_reset_is_rejected_before_runtime_ready():
    gate = InputRuntimeReadinessGate()
    api = SimpleNamespace(input_runtime_readiness_gate=gate)

    with pytest.raises(InputRuntimeRecoveryError) as error:
        await reset_runtime_session(api, "session")

    assert error.value.reason_code == "input_runtime_not_ready"
    assert error.value.fatal is False
    assert gate.state == InputRuntimeLifecycleState.STOPPED
