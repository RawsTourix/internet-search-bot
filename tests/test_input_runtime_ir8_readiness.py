from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from src.input_runtime.recovery import (
    InputRuntimeLifecycleState,
    InputRuntimeReadinessGate,
    InputRuntimeRecoveryCoordinator,
    InputRuntimeRecoveryError,
    InputRuntimeRecoveryReport,
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
    # Recovery itself never opens READY; production composition still has to
    # connect MCP and install runner ownership first.
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
