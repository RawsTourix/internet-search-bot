from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.api import input_runtime_recovery as lifecycle
from src.input_runtime.recovery import (
    InputRuntimeLifecycleState,
    InputRuntimeReadinessGate,
    InputRuntimeRecoveryError,
    InputRuntimeRecoveryPlan,
    InputRuntimeRecoveryReport,
)
from src.api.input_runtime_recovery_dependencies import RecoveredRuntimeDependencies


class FakeAPIError(RuntimeError):
    pass


class FakeRecovery:
    def __init__(self, *, entered=None, release=None, error=None):
        self.entered = entered
        self.release = release
        self.error = error

    async def recover(self):
        if self.entered is not None:
            self.entered.set()
        if self.release is not None:
            await self.release.wait()
        if self.error is not None:
            raise self.error
        return InputRuntimeRecoveryPlan(
            sessions=(),
            report=InputRuntimeRecoveryReport(),
        )


class FakeMCP:
    def __init__(self, events):
        self.events = events
        self.connected = False
        self.cleaned = False

    async def connect_to_servers(self, configs):
        self.events.append("mcp_connect")
        self.connected = True

    async def cleanup(self):
        self.events.append("mcp_cleanup")
        self.cleaned = True


class FakeCoordinator:
    def __init__(self, events):
        self.events = events

    async def shutdown(self):
        self.events.append("coordinator_shutdown")


class EmptyIngress:
    async def commit_ready_drafts(self):
        return ()


class EmptyPresentationStore:
    async def expire_stale_reservations(self, *, timeout_seconds):
        return ()

    async def list_recoverable(self):
        return ()


class EmptyOutputStore:
    async def reconcile_stale_claims(self, *, timeout_seconds):
        return ()

    async def list_recoverable(self):
        return ()


class FakeApi:
    def __init__(self):
        self.events = []
        self.input_runtime_readiness_gate = InputRuntimeReadinessGate()
        self.input_runtime_recovery = FakeRecovery()
        self.input_runtime_recovery_plan = None
        self.input_runtime_recovery_report = None
        self.input_runtime_recovery_dependencies = RecoveredRuntimeDependencies(
            active_plan_states={}
        )
        self._ir8_blocked_cycles = {}
        self._ir8_runner_tasks = set()
        self.artifact_services = SimpleNamespace(
            workspace_manager=object(),
            delivery_store=object(),
        )
        self.artifact_config = SimpleNamespace(
            workspace_ttl_seconds=10,
            delivery_claim_timeout_seconds=10,
        )
        self.ingress_services = SimpleNamespace(
            ingress_service=EmptyIngress(),
            presentation_store=EmptyPresentationStore(),
        )
        self.interaction_config = SimpleNamespace(
            input_presentation=SimpleNamespace(reservation_timeout_seconds=10),
            output_runtime=SimpleNamespace(delivery_claim_timeout_seconds=10),
        )
        self.output_store = EmptyOutputStore()
        self.mcp_client = FakeMCP(self.events)
        self.execution_coordinator = FakeCoordinator(self.events)
        self.server_configs = []

    async def start(self):
        raise AssertionError("installer must replace start")

    async def stop(self):
        raise AssertionError("installer must replace stop")

    async def submit_input(self, *args, **kwargs):
        return "submit"

    async def admit_committed_batch(self, *args, **kwargs):
        return "admit"

    async def start_admitted_cycle(self, *args, **kwargs):
        return "start-cycle"

    async def resume_admitted_cycle(self, *args, **kwargs):
        return "resume-cycle"

    async def call_agent_batch(self, *args, **kwargs):
        return "call-batch"

    async def call_agent(self, *args, **kwargs):
        return "call"


class NullLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def exception(self, *args, **kwargs):
        pass


def fake_module(api):
    async def cleanup_stale(*args, **kwargs):
        return ()

    async def recover_delivery(*args, **kwargs):
        return ()

    return SimpleNamespace(
        Api=FakeApi,
        API=api,
        logger=NullLogger(),
        APIError=FakeAPIError,
        cleanup_stale_artifact_workspaces=cleanup_stale,
        recover_stale_delivery_claims=recover_delivery,
    )


@pytest.mark.asyncio
async def test_api_start_recovery_connect_install_ready_order(monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    api = FakeApi()
    api.input_runtime_recovery = FakeRecovery(entered=entered, release=release)
    module = fake_module(api)
    events = api.events

    async def dependencies(owner, plan):
        events.append("dependencies")
        return RecoveredRuntimeDependencies(active_plan_states={})

    async def no_legacy(*args, **kwargs):
        events.append("output_reconcile")

    async def install(owner, plan, **kwargs):
        assert owner.mcp_client.connected is True
        assert owner.input_runtime_readiness_gate.is_ready is False
        events.append("runtime_install")

    async def no_cancel(owner):
        events.append("runner_cancel")

    monkeypatch.setattr(lifecycle, "validate_recovered_runtime_dependencies", dependencies)
    monkeypatch.setattr(lifecycle, "reconcile_unclaimable_legacy_ready", no_legacy)
    monkeypatch.setattr(lifecycle, "_install_recovered_runtime", install)
    monkeypatch.setattr(lifecycle, "_cancel_recovered_tasks", no_cancel)

    lifecycle.install_input_runtime_recovery_lifecycle(module)
    task = asyncio.create_task(api.start())
    await entered.wait()
    assert api.input_runtime_readiness_gate.state == InputRuntimeLifecycleState.RECOVERING
    assert "mcp_connect" not in events

    release.set()
    await task
    assert api.input_runtime_readiness_gate.state == InputRuntimeLifecycleState.READY
    assert events.index("dependencies") < events.index("mcp_connect")
    assert events.index("mcp_connect") < events.index("runtime_install")

    await api.stop()
    assert api.input_runtime_readiness_gate.state == InputRuntimeLifecycleState.STOPPED
    assert events.index("runner_cancel") < events.index("coordinator_shutdown")
    assert events.index("coordinator_shutdown") < events.index("mcp_cleanup")


@pytest.mark.asyncio
async def test_api_start_recovery_failure_never_connects_mcp(monkeypatch):
    api = FakeApi()
    api.input_runtime_recovery = FakeRecovery(
        error=InputRuntimeRecoveryError("corrupt_history")
    )
    module = fake_module(api)

    async def no_legacy(*args, **kwargs):
        return None

    monkeypatch.setattr(lifecycle, "reconcile_unclaimable_legacy_ready", no_legacy)
    lifecycle.install_input_runtime_recovery_lifecycle(module)

    with pytest.raises(FakeAPIError):
        await api.start()
    assert api.input_runtime_readiness_gate.state == InputRuntimeLifecycleState.FAILED
    assert api.mcp_client.connected is False
    assert "mcp_connect" not in api.events
