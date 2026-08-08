from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.api.input_runtime_controls import request_runtime_continue
from src.api.input_runtime_recovery import _install_recovered_runtime
from src.input_runtime import (
    CheckpointName,
    CycleInputApplier,
    CycleStatus,
    InputAdmissionAction,
    InputAdmissionService,
    InputRuntimeConfigType,
    create_filesystem_input_runtime_repositories,
)
from src.input_runtime.recovery import InputRuntimeReadinessGate, RecoveryDisposition
from src.input_runtime.recovery_hardening import InputRuntimeRecoveryCoordinator
from src.runtime import ActiveAgentCycle, SessionExecutionCoordinator
from src.storage import StorageConfigType

NOW = datetime(2026, 8, 8, 21, 15, tzinfo=timezone.utc)


@dataclass
class Batch:
    input_batch_id: str
    sequence_number: int
    session_id: str = "session"
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


class FreshMCPRuntime:
    def __init__(self) -> None:
        self.pending: dict[str, ActiveAgentCycle] = {}
        self.resume_calls: list[tuple[str, str]] = []

    def install_recovered_cycle(self, cycle: ActiveAgentCycle) -> None:
        assert cycle.session_id not in self.pending
        self.pending[cycle.session_id] = cycle

    def can_resume_controlled_cycle(self, *, session_id: str, cycle_id: str) -> bool:
        cycle = self.pending.get(session_id)
        return cycle is not None and cycle.cycle_id == cycle_id

    async def resume_controlled_cycle(self, *, session_id: str, cycle_id: str, **kwargs):
        cycle = self.pending[session_id]
        assert cycle.cycle_id == cycle_id
        assert cycle.active_context_revision_id is not None
        assert cycle.messages_for_llm
        self.resume_calls.append((session_id, cycle_id))
        # This test proves the fresh process resumes the exact recovered cycle.
        # The existing IR-4/IR-5 suites own semantic CP-RESUME execution; return
        # None here so no fake final/status projection is introduced.
        return None


class NullLogger:
    def exception(self, *args, **kwargs) -> None:
        pass


def make_runtime(tmp_path, reader):
    repositories = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )
    coordinator = SessionExecutionCoordinator()
    service = InputAdmissionService(
        config=InputRuntimeConfigType(),
        repositories=repositories,
        committed_batches=reader,
        wake_coordinator=coordinator,
        cycle_id_factory=lambda: "cycle",
        clock=lambda: NOW,
        payload_size_resolver=lambda batch: batch.payload_size,
    )
    return repositories, coordinator, service


async def seed_running(tmp_path, reader):
    repositories, _, service = make_runtime(tmp_path, reader)
    initial = await service.admit_committed_batch("initial", session_id="session")
    cycle = ActiveAgentCycle(
        cycle_id=initial.target_cycle_id,
        session_id="session",
        original_user_request="initial",
        messages_for_llm=[
            {"role": "system", "content": "system"},
            {
                "role": "user",
                "content": json.dumps(
                    {"type": "user_request", "user_request": "initial"}
                ),
            },
        ],
        cycle_trace=[],
        original_user_message_index=1,
        original_input_batch_id="initial",
        input_runtime_generation=0,
    )
    applier = CycleInputApplier(
        config=service.config,
        repositories=repositories,
        committed_batches=reader,
        clock=lambda: NOW,
    )
    await applier.ensure_initial_context(
        session_id="session",
        cycle_id=initial.target_cycle_id,
        generation=0,
        checkpoint=CheckpointName.RESUME,
        active_cycle=cycle,
        input_batch_id="initial",
    )
    return repositories, service, initial


async def recover_fresh(tmp_path, reader):
    repositories, coordinator, service = make_runtime(tmp_path, reader)
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
    api = SimpleNamespace(
        input_runtime_readiness_gate=gate,
        input_runtime_recovery_dependencies=SimpleNamespace(active_plan_states={}),
        _ir8_blocked_cycles={},
        _ir8_runner_tasks=set(),
        input_runtime_repositories=repositories,
        input_admission_service=service,
        execution_coordinator=coordinator,
        mcp_client=mcp,
    )

    async def resolve_batch(input_batch_id, *, session_id):
        assert session_id == "session"
        await reader.get_committed(input_batch_id)
        return SimpleNamespace(client_type=None, locale="ru"), SimpleNamespace()

    api._resolve_batch_and_capability = resolve_batch
    api._cycle_status_from_result = lambda result: CycleStatus.RUNNING

    async def no_final(**kwargs):
        return None

    api._assemble_final_if_needed = no_final

    async def must_not_start(owner, outcome):
        raise AssertionError("paused/waiting recovery must not auto-start a runner")

    await _install_recovered_runtime(
        api,
        plan,
        original_start_admitted_cycle=must_not_start,
        logger=NullLogger(),
    )
    gate.mark_ready()
    return api, plan


@pytest.mark.asyncio
async def test_paused_fresh_continue_uses_same_cycle_and_recovered_context(tmp_path):
    reader = Reader(Batch("initial", 1))
    _, service, initial = await seed_running(tmp_path, reader)
    await service.control_service.request_pause(
        session_id="session",
        idempotency_key="pause",
        source_client_type="test",
        reason="pause",
    )

    api, plan = await recover_fresh(tmp_path, reader)
    assert len(plan.sessions) == 1
    assert plan.sessions[0].disposition == RecoveryDisposition.PAUSED
    recovered = api.mcp_client.pending["session"]
    assert recovered.cycle_id == initial.target_cycle_id
    recovered_revision = recovered.active_context_revision_id
    recovered_messages = list(recovered.messages_for_llm)

    reader.add(Batch("queued", 2))
    queued = await api.input_admission_service.admit_committed_batch(
        "queued",
        session_id="session",
    )
    assert queued.target_cycle_id == initial.target_cycle_id
    assert queued.action == InputAdmissionAction.QUEUE_PAUSED

    result = await request_runtime_continue(
        api,
        session_id="session",
        idempotency_key="continue",
        source_client_type="test",
        reason="continue",
    )
    assert result.outcome.command.target_cycle_id == initial.target_cycle_id
    assert result.agent_result is None
    assert api.mcp_client.resume_calls == [("session", initial.target_cycle_id)]
    installed = api.mcp_client.pending["session"]
    assert installed.cycle_id == initial.target_cycle_id
    assert installed.active_context_revision_id == recovered_revision
    assert installed.messages_for_llm == recovered_messages
    inbox = await api.input_runtime_repositories.inbox.list_for_cycle(
        initial.target_cycle_id
    )
    assert [item.input_batch_id for item in inbox] == ["queued"]
    assert [item.cycle_sequence for item in inbox] == [1]


@pytest.mark.asyncio
async def test_waiting_fresh_reply_admits_resume_waiting_to_same_recovered_cycle(tmp_path):
    reader = Reader(Batch("initial", 1))
    repositories, _, initial = await seed_running(tmp_path, reader)
    snapshot = await repositories.snapshots.get(initial.target_cycle_id)
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

    api, plan = await recover_fresh(tmp_path, reader)
    assert plan.sessions[0].disposition == RecoveryDisposition.WAITING
    installed = api.mcp_client.pending["session"]
    assert installed.cycle_id == initial.target_cycle_id
    assert installed.waiting_question == "Which option?"

    reader.add(Batch("reply", 2))
    reply = await api.input_admission_service.admit_committed_batch(
        "reply",
        session_id="session",
    )
    assert reply.action == InputAdmissionAction.RESUME_WAITING
    assert reply.target_cycle_id == initial.target_cycle_id
    assert reply.admission.cycle_sequence == 1
    assert api.mcp_client.pending["session"].cycle_id == initial.target_cycle_id
