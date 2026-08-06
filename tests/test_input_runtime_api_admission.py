from __future__ import annotations

import os

os.environ["AGENT_CONFIG_PATH"] = "src/api/mcp.config.example"

from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.api.api import Api
from src.core.models import AgentResult, AgentStatus, ClientType
from src.input_runtime import (
    CycleStatus,
    InputAdmissionAction,
    InputAdmissionService,
    InputRuntimeConfigType,
    RuntimeHandoffState,
    create_filesystem_input_runtime_repositories,
)
from src.runtime import SessionExecutionCoordinator
from src.storage import StorageConfigType


NOW = datetime(2026, 8, 6, tzinfo=timezone.utc)


@dataclass
class Batch:
    input_batch_id: str
    session_id: str
    client_type: ClientType = ClientType.CLI
    capability_snapshot: object = object()
    locale: str = "ru"
    source_event_ids: tuple[str, ...] = ("evt_" + "1" * 32,)
    content_fingerprint: str = "sha256:" + "2" * 64
    committed_at: datetime = NOW
    artifact_refs: tuple[str, ...] = ()
    response_route: object = None

    def model_dump_json(self):
        return '{"batch":true}'


class Reader:
    def __init__(self, *items):
        self.items = {item.input_batch_id: item for item in items}
        self.failure: Exception | None = None

    async def get_committed(self, input_batch_id):
        if self.failure is not None:
            raise self.failure
        return self.items[input_batch_id]


class MCP:
    def __init__(self):
        self.calls = []

    async def process_query(self, message, **kwargs):
        self.calls.append(kwargs)
        return AgentResult(
            content="done",
            status=AgentStatus.DONE,
            session_id=kwargs["session_id"],
            cycle_id=kwargs["cycle_id_override"],
        )


def make_api(tmp_path, *batches):
    api = object.__new__(Api)
    api.execution_coordinator = SessionExecutionCoordinator()
    api.input_runtime_config = InputRuntimeConfigType()
    api.input_runtime_repositories = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )
    reader = Reader(*batches)
    api.input_admission_service = InputAdmissionService(
        config=api.input_runtime_config,
        repositories=api.input_runtime_repositories,
        committed_batches=reader,
        wake_coordinator=api.execution_coordinator,
        cycle_id_factory=lambda: "admitted-cycle",
        clock=lambda: NOW,
        payload_size_resolver=lambda _batch: 10,
    )
    api.ingress_services = SimpleNamespace(batch_store=reader)
    api.mcp_client = MCP()
    api.interaction_config = SimpleNamespace(
        output_runtime=SimpleNamespace(enabled=False),
        telegram_output=SimpleNamespace(
            prefer_document_groups=False,
            status_message_editing=False,
        ),
    )
    api.output_assembler = SimpleNamespace()
    return api


@pytest.mark.asyncio
async def test_api_uses_admission_cycle_id_and_marks_initial_applied(tmp_path):
    batch = Batch("initial", "session")
    api = make_api(tmp_path, batch)
    outcome = await api.admit_committed_batch("initial", session_id="session")
    assert outcome.action == InputAdmissionAction.START_CYCLE
    result = await api.start_admitted_cycle(outcome)
    assert result is not None
    assert result.cycle_id == "admitted-cycle"
    assert len(api.mcp_client.calls) == 1
    assert api.mcp_client.calls[0]["cycle_id_override"] == "admitted-cycle"
    assert api.mcp_client.calls[0]["input_batch"] is batch
    admission = await api.input_runtime_repositories.admissions.get_by_input_batch_id(
        "initial"
    )
    assert admission.state.value == "applied"
    marker = await api.input_admission_service.get_runtime_handoff(admission)
    assert marker is not None
    assert marker.state == RuntimeHandoffState.COMPLETED


@pytest.mark.asyncio
async def test_pre_handoff_resolution_failure_remains_retryable(tmp_path):
    batch = Batch("initial", "session")
    api = make_api(tmp_path, batch)
    outcome = await api.admit_committed_batch("initial", session_id="session")
    api.ingress_services.batch_store.failure = RuntimeError("resolution failed")

    with pytest.raises(Exception):
        await api.start_admitted_cycle(outcome)
    admission = await api.input_runtime_repositories.admissions.get_by_input_batch_id(
        "initial"
    )
    assert admission.state.value == "admitted"
    assert await api.input_admission_service.get_runtime_handoff(admission) is None
    assert len(api.mcp_client.calls) == 0

    api.ingress_services.batch_store.failure = None
    duplicate = await api.admit_committed_batch("initial", session_id="session")
    assert duplicate.action == InputAdmissionAction.DUPLICATE
    assert duplicate.should_start_runner is True
    result = await api.start_admitted_cycle(duplicate)
    assert result is not None
    assert len(api.mcp_client.calls) == 1


class SideEffectThenFailMCP:
    def __init__(self):
        self.call_count = 0
        self.side_effect_count = 0

    async def process_query(self, message, **kwargs):
        self.call_count += 1
        self.side_effect_count += 1
        raise RuntimeError("ambiguous after side effect")


@pytest.mark.asyncio
async def test_post_handoff_side_effect_failure_is_not_replayed(tmp_path):
    batch = Batch("initial", "session")
    api = make_api(tmp_path, batch)
    api.mcp_client = SideEffectThenFailMCP()
    outcome = await api.admit_committed_batch("initial", session_id="session")

    with pytest.raises(Exception):
        await api.start_admitted_cycle(outcome)
    admission = await api.input_runtime_repositories.admissions.get_by_input_batch_id(
        "initial"
    )
    marker = await api.input_admission_service.get_runtime_handoff(admission)
    assert marker is not None
    assert marker.state == RuntimeHandoffState.AMBIGUOUS
    state = await api.input_runtime_repositories.sessions.get("session")
    assert state is not None
    assert state.cycle_status == CycleStatus.INTERRUPTED

    duplicate = await api.admit_committed_batch("initial", session_id="session")
    assert duplicate.should_start_runner is False
    assert await api.start_admitted_cycle(duplicate) is None
    assert api.mcp_client.call_count == 1
    assert api.mcp_client.side_effect_count == 1


@pytest.mark.asyncio
async def test_post_invocation_persistence_failure_is_not_replayed(tmp_path):
    batch = Batch("initial", "session")
    api = make_api(tmp_path, batch)
    outcome = await api.admit_committed_batch("initial", session_id="session")

    async def fail_persistence(_admission):
        raise OSError("simulated admission persistence failure")

    api.input_admission_service.mark_initial_batch_applied = fail_persistence
    with pytest.raises(Exception):
        await api.start_admitted_cycle(outcome)
    assert len(api.mcp_client.calls) == 1

    admission = await api.input_runtime_repositories.admissions.get_by_input_batch_id(
        "initial"
    )
    marker = await api.input_admission_service.get_runtime_handoff(admission)
    assert marker is not None
    assert marker.state == RuntimeHandoffState.AMBIGUOUS
    duplicate = await api.admit_committed_batch("initial", session_id="session")
    assert duplicate.should_start_runner is False
    assert await api.start_admitted_cycle(duplicate) is None
    assert len(api.mcp_client.calls) == 1


class SequenceMCP:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def process_query(self, message, **kwargs):
        self.calls.append(kwargs)
        status, content, can_resume = self.results.pop(0)
        return AgentResult(
            content=content,
            status=status,
            session_id=kwargs["session_id"],
            cycle_id=kwargs["cycle_id_override"],
            can_resume=can_resume,
        )


@pytest.mark.asyncio
async def test_waiting_user_compatibility_resumes_same_cycle_once(tmp_path):
    initial = Batch("initial", "session")
    reply = Batch("reply", "session")
    api = make_api(tmp_path, initial, reply)
    api.mcp_client = SequenceMCP([
        (AgentStatus.WAITING_USER, "question", False),
        (AgentStatus.DONE, "continued", False),
    ])

    first = await api.admit_committed_batch("initial", session_id="session")
    waiting_result = await api.start_admitted_cycle(first)
    assert waiting_result.status == AgentStatus.WAITING_USER

    continuation = await api.admit_committed_batch("reply", session_id="session")
    assert continuation.action == InputAdmissionAction.RESUME_WAITING
    result = await api.resume_admitted_cycle(continuation)
    assert result is not None
    assert result.content == "continued"
    assert [call["cycle_id_override"] for call in api.mcp_client.calls] == [
        "admitted-cycle",
        "admitted-cycle",
    ]
    admission = await api.input_runtime_repositories.admissions.get_by_input_batch_id(
        "reply"
    )
    assert admission.state.value == "applied"
    marker = await api.input_admission_service.get_runtime_handoff(admission)
    assert marker is not None
    assert marker.state == RuntimeHandoffState.COMPLETED
    inbox = await api.input_runtime_repositories.inbox.list_for_cycle(
        "admitted-cycle"
    )
    assert len(inbox) == 1
    assert inbox[0].state.value == "applied"


class WaitingSideEffectThenFailMCP:
    def __init__(self):
        self.calls = 0

    async def process_query(self, message, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return AgentResult(
                content="question",
                status=AgentStatus.WAITING_USER,
                session_id=kwargs["session_id"],
                cycle_id=kwargs["cycle_id_override"],
            )
        raise RuntimeError("ambiguous waiting continuation")


@pytest.mark.asyncio
async def test_waiting_ambiguous_handoff_is_not_requeued_for_blind_replay(tmp_path):
    initial = Batch("initial", "session")
    reply = Batch("reply", "session")
    api = make_api(tmp_path, initial, reply)
    api.mcp_client = WaitingSideEffectThenFailMCP()

    first = await api.admit_committed_batch("initial", session_id="session")
    await api.start_admitted_cycle(first)
    continuation = await api.admit_committed_batch("reply", session_id="session")
    with pytest.raises(Exception):
        await api.resume_admitted_cycle(continuation)

    admission = await api.input_runtime_repositories.admissions.get_by_input_batch_id(
        "reply"
    )
    marker = await api.input_admission_service.get_runtime_handoff(admission)
    assert marker is not None
    assert marker.state == RuntimeHandoffState.AMBIGUOUS
    inbox = await api.input_runtime_repositories.inbox.list_for_cycle(
        "admitted-cycle"
    )
    assert len(inbox) == 1
    assert inbox[0].state.value == "applying"

    duplicate = await api.admit_committed_batch("reply", session_id="session")
    assert duplicate.should_wake_runner is False
    assert await api.resume_admitted_cycle(duplicate) is None
    assert api.mcp_client.calls == 2


@pytest.mark.asyncio
async def test_interrupted_outcome_does_not_automatically_replay_runner(tmp_path):
    initial = Batch("initial", "session")
    addition = Batch("addition", "session")
    api = make_api(tmp_path, initial, addition)
    api.mcp_client = SequenceMCP([
        (AgentStatus.ERROR, "interrupted", True),
    ])

    first = await api.admit_committed_batch("initial", session_id="session")
    result = await api.start_admitted_cycle(first)
    assert result.can_resume is True
    interrupted = await api.admit_committed_batch(
        "addition",
        session_id="session",
    )
    assert interrupted.action == InputAdmissionAction.RESUME_INTERRUPTED
    assert interrupted.should_wake_runner is True
    assert await api.resume_admitted_cycle(interrupted) is None
    assert len(api.mcp_client.calls) == 1
