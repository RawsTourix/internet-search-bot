from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.api.input_runtime_recovery import (
    _cancel_recovered_tasks,
    _install_recovered_runtime,
)
from src.input_runtime import (
    InputAdmissionService,
    InputRuntimeConfigType,
    create_filesystem_input_runtime_repositories,
)
from src.input_runtime.recovery import InputRuntimeReadinessGate
from src.input_runtime.recovery_hardening import InputRuntimeRecoveryCoordinator
from src.runtime import SessionExecutionCoordinator
from src.storage import StorageConfigType

NOW = datetime(2026, 8, 8, 20, 45, tzinfo=timezone.utc)


@dataclass
class Batch:
    input_batch_id: str = "initial"
    session_id: str = "session"
    sequence_number: int = 1
    payload_size: int = 10
    text_parts: list[object] = field(default_factory=list)
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
    def __init__(self, batch: Batch) -> None:
        self.batch = batch

    async def get_committed(self, input_batch_id: str):
        if input_batch_id != self.batch.input_batch_id:
            raise KeyError(input_batch_id)
        return self.batch

    async def list_committed_for_recovery(self):
        return (self.batch,)


class FreshMCPRuntime:
    def __init__(self) -> None:
        self.connected = False
        self.pending_cycles: dict[str, object] = {}

    async def connect(self) -> None:
        self.connected = True

    def install_recovered_cycle(self, cycle) -> None:
        assert self.connected is True
        assert cycle.session_id not in self.pending_cycles
        self.pending_cycles[cycle.session_id] = cycle


class NullLogger:
    def exception(self, *args, **kwargs) -> None:
        pass


@pytest.mark.asyncio
async def test_safe_recovered_runner_is_owned_after_connect_before_ready_and_no_duplicate(
    tmp_path,
):
    reader = Reader(Batch())

    # Process A durable state: committed input exists but admission never ran.
    # Process B is completely fresh: repositories, service, coordinator and MCP
    # session memory are all new objects over the same durable root.
    repositories = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )
    coordinator = SessionExecutionCoordinator()
    service = InputAdmissionService(
        config=InputRuntimeConfigType(),
        repositories=repositories,
        committed_batches=reader,
        wake_coordinator=coordinator,
        cycle_id_factory=lambda: "cycle-recovered",
        clock=lambda: NOW,
        payload_size_resolver=lambda batch: batch.payload_size,
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
    assert gate.is_ready is False
    assert len(plan.sessions) == 1
    session_plan = plan.sessions[0]
    assert session_plan.should_auto_schedule is True

    mcp = FreshMCPRuntime()
    await mcp.connect()
    api = SimpleNamespace(
        input_runtime_readiness_gate=gate,
        input_runtime_recovery_dependencies=SimpleNamespace(
            active_plan_states={}
        ),
        _ir8_blocked_cycles={},
        _ir8_runner_tasks=set(),
        execution_coordinator=coordinator,
        mcp_client=mcp,
    )
    runner_entered = asyncio.Event()
    release_runner = asyncio.Event()

    async def production_start(owner, outcome):
        assert owner.mcp_client.connected is True
        assert owner.input_runtime_readiness_gate.is_ready is True
        admission = outcome.admission
        assert admission is not None
        async with owner.execution_coordinator.admitted_run_lease(
            session_id=admission.session_id,
            input_batch_id=admission.input_batch_id,
            cycle_id=admission.target_cycle_id,
            expected_generation=admission.admitted_generation,
        ) as acquired:
            assert acquired is True
            runner_entered.set()
            await release_runner.wait()

    await _install_recovered_runtime(
        api,
        plan,
        original_start_admitted_cycle=production_start,
        logger=NullLogger(),
    )
    # Task is installed and reservation exists, but the gate still prevents any
    # AgentCycle/LLM execution before the composition explicitly opens READY.
    assert runner_entered.is_set() is False
    reserved = await coordinator.snapshot("session")
    assert reserved.reserved_cycle_id == "cycle-recovered"
    assert len(api._ir8_runner_tasks) == 1

    gate.mark_ready()
    await runner_entered.wait()

    # An ordinary addition arriving immediately after READY cannot acquire a
    # second runner for the recovered cycle. It remains FIFO work for runner #1.
    async with coordinator.admitted_run_lease(
        session_id="session",
        input_batch_id="immediate-addition",
        cycle_id="cycle-recovered",
        expected_generation=0,
    ) as acquired:
        assert acquired is False

    release_runner.set()
    tasks = tuple(api._ir8_runner_tasks)
    await asyncio.gather(*tasks)
    assert all(task.done() for task in tasks)


@pytest.mark.asyncio
async def test_shutdown_cancels_owned_recovered_runner_before_new_start(tmp_path):
    reader = Reader(Batch())
    repositories = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )
    coordinator = SessionExecutionCoordinator()
    service = InputAdmissionService(
        config=InputRuntimeConfigType(),
        repositories=repositories,
        committed_batches=reader,
        wake_coordinator=coordinator,
        cycle_id_factory=lambda: "cycle-recovered",
        clock=lambda: NOW,
        payload_size_resolver=lambda batch: batch.payload_size,
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
    mcp = FreshMCPRuntime()
    await mcp.connect()
    api = SimpleNamespace(
        input_runtime_readiness_gate=gate,
        input_runtime_recovery_dependencies=SimpleNamespace(
            active_plan_states={}
        ),
        _ir8_blocked_cycles={},
        _ir8_runner_tasks=set(),
        execution_coordinator=coordinator,
        mcp_client=mcp,
    )
    entered = asyncio.Event()

    async def blocked_start(owner, outcome):
        admission = outcome.admission
        async with owner.execution_coordinator.admitted_run_lease(
            session_id=admission.session_id,
            input_batch_id=admission.input_batch_id,
            cycle_id=admission.target_cycle_id,
            expected_generation=admission.admitted_generation,
        ) as acquired:
            assert acquired is True
            entered.set()
            await asyncio.Event().wait()

    await _install_recovered_runtime(
        api,
        plan,
        original_start_admitted_cycle=blocked_start,
        logger=NullLogger(),
    )
    gate.mark_ready()
    await entered.wait()
    gate.begin_stopping()
    await _cancel_recovered_tasks(api)
    await coordinator.shutdown()
    assert gate.is_ready is False
    assert all(task.done() for task in api._ir8_runner_tasks)
    with pytest.raises(RuntimeError, match="shutting down"):
        async with coordinator.admitted_run_lease(
            session_id="session",
            input_batch_id="new",
            cycle_id="cycle-new",
            expected_generation=0,
        ):
            pass
