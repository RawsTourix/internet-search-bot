from __future__ import annotations

import asyncio
import inspect
import os
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

os.environ["AGENT_CONFIG_PATH"] = "src/api/mcp.config.example"

from src.api.api import Api
from src.core.models import AgentResult, AgentStatus, ClientType
from src.input_runtime import (
    AdmissionKind,
    CycleStatus,
    InboxState,
    InputAdmissionRecord,
    InputAdmissionService,
    InputRuntimeConfigType,
    RuntimeHandoffRecord,
    RuntimeHandoffRepository,
    RuntimeHandoffState,
    create_filesystem_input_runtime_repositories,
)
from src.input_runtime.errors import InputRuntimeConflictError
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

    async def get_committed(self, input_batch_id):
        return self.items[input_batch_id]


class ImmediateMCP:
    def __init__(self, status=AgentStatus.DONE):
        self.calls = 0
        self.status = status

    async def process_query(self, message, **kwargs):
        self.calls += 1
        return AgentResult(
            content="result",
            status=self.status,
            session_id=kwargs["session_id"],
            cycle_id=kwargs["cycle_id_override"],
        )


class BlockingMCP:
    def __init__(self):
        self.calls = 0
        self.entered = asyncio.Event()

    async def process_query(self, message, **kwargs):
        self.calls += 1
        self.entered.set()
        await asyncio.Event().wait()


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
    api.mcp_client = ImmediateMCP()
    api.interaction_config = SimpleNamespace(
        output_runtime=SimpleNamespace(enabled=False),
        telegram_output=SimpleNamespace(
            prefer_document_groups=False,
            status_message_editing=False,
        ),
    )
    api.output_assembler = SimpleNamespace()
    return api


async def cancel_task(task):
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def prepare_waiting(api):
    api.mcp_client = ImmediateMCP(status=AgentStatus.WAITING_USER)
    first = await api.admit_committed_batch("initial", session_id="session")
    result = await api.start_admitted_cycle(first)
    assert result.status == AgentStatus.WAITING_USER
    return await api.admit_committed_batch("reply", session_id="session")


async def marker_for(api, input_batch_id):
    admission = (
        await api.input_runtime_repositories.admissions.get_by_input_batch_id(
            input_batch_id
        )
    )
    assert admission is not None
    return admission, await api.input_admission_service.get_runtime_handoff(
        admission
    )


@pytest.mark.asyncio
async def test_initial_task_cancelled_during_process_query_is_ambiguous_and_not_replayed(
    tmp_path,
):
    api = make_api(tmp_path, Batch("initial", "session"))
    blocker = BlockingMCP()
    api.mcp_client = blocker
    outcome = await api.admit_committed_batch("initial", session_id="session")

    cleanup_entered = asyncio.Event()
    cleanup_release = asyncio.Event()
    original_mark = api.input_admission_service.mark_runtime_handoff_ambiguous

    async def slow_mark(*args, **kwargs):
        cleanup_entered.set()
        await cleanup_release.wait()
        return await original_mark(*args, **kwargs)

    api.input_admission_service.mark_runtime_handoff_ambiguous = slow_mark
    task = asyncio.create_task(api.start_admitted_cycle(outcome))
    await blocker.entered.wait()
    task.cancel()
    await cleanup_entered.wait()
    task.cancel()
    cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    admission, marker = await marker_for(api, "initial")
    assert marker is not None
    assert marker.state == RuntimeHandoffState.AMBIGUOUS
    state = await api.input_runtime_repositories.sessions.get("session")
    assert state is not None
    assert state.cycle_status == CycleStatus.INTERRUPTED

    duplicate = await api.admit_committed_batch("initial", session_id="session")
    assert duplicate.should_start_runner is False
    assert await api.start_admitted_cycle(duplicate) is None
    assert blocker.calls == 1
    assert admission.state.value == "admitted"


@pytest.mark.asyncio
async def test_initial_task_cancelled_after_marker_before_invocation_is_not_replayed(
    tmp_path,
):
    api = make_api(tmp_path, Batch("initial", "session"))
    outcome = await api.admit_committed_batch("initial", session_id="session")
    marker_written = asyncio.Event()
    hold = asyncio.Event()
    original_begin = api.input_admission_service.begin_runtime_handoff

    async def begin_then_hold(*args, **kwargs):
        owns = await original_begin(*args, **kwargs)
        marker_written.set()
        await hold.wait()
        return owns

    api.input_admission_service.begin_runtime_handoff = begin_then_hold
    task = asyncio.create_task(api.start_admitted_cycle(outcome))
    await marker_written.wait()
    await cancel_task(task)

    _, marker = await marker_for(api, "initial")
    assert marker is not None
    assert marker.state == RuntimeHandoffState.AMBIGUOUS
    state = await api.input_runtime_repositories.sessions.get("session")
    assert state is not None
    assert state.cycle_status == CycleStatus.INTERRUPTED

    duplicate = await api.admit_committed_batch("initial", session_id="session")
    assert duplicate.should_start_runner is False
    assert await api.start_admitted_cycle(duplicate) is None
    assert api.mcp_client.calls == 0


@pytest.mark.asyncio
async def test_initial_cancellation_before_marker_remains_retryable(tmp_path):
    api = make_api(tmp_path, Batch("initial", "session"))
    outcome = await api.admit_committed_batch("initial", session_id="session")
    entered = asyncio.Event()
    original_resolve = api._resolve_batch_and_capability

    async def blocked_resolution(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    api._resolve_batch_and_capability = blocked_resolution
    task = asyncio.create_task(api.start_admitted_cycle(outcome))
    await entered.wait()
    await cancel_task(task)

    admission, marker = await marker_for(api, "initial")
    assert marker is None
    assert admission.state.value == "admitted"

    api._resolve_batch_and_capability = original_resolve
    original_process_query = api.mcp_client.process_query

    async def checkpointing_process_query(message, **kwargs):
        from src.input_runtime import CheckpointAction, CheckpointName
        from src.runtime import ActiveAgentCycle

        state = await api.input_runtime_repositories.sessions.get(kwargs["session_id"])
        assert state is not None
        active = ActiveAgentCycle(
            cycle_id=kwargs["cycle_id_override"],
            session_id=kwargs["session_id"],
            original_user_request="initial",
            messages_for_llm=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": '{"type":"user_request"}'},
            ],
            cycle_trace=[],
            original_user_message_index=1,
            original_input_batch_id=kwargs["input_batch"].input_batch_id,
            input_runtime_generation=state.generation,
        )
        checkpoint = await api.input_admission_service.checkpoint_service.run_checkpoint(
            checkpoint=CheckpointName.RESUME,
            active_cycle=active,
            desired_status=CycleStatus.RUNNING,
        )
        assert checkpoint.action != CheckpointAction.INTERRUPT, checkpoint.reason_code
        return await original_process_query(message, **kwargs)

    api.mcp_client.process_query = checkpointing_process_query
    duplicate = await api.admit_committed_batch("initial", session_id="session")
    assert duplicate.should_start_runner is True
    assert await api.start_admitted_cycle(duplicate) is not None
    assert api.mcp_client.calls == 1


@pytest.mark.asyncio
async def test_waiting_cancellation_after_claim_before_marker_requeues_claim(tmp_path):
    api = make_api(
        tmp_path,
        Batch("initial", "session"),
        Batch("reply", "session"),
    )
    continuation = await prepare_waiting(api)
    entered = asyncio.Event()

    async def blocked_resolution(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    api._resolve_batch_and_capability = blocked_resolution
    task = asyncio.create_task(api.resume_admitted_cycle(continuation))
    await entered.wait()
    await cancel_task(task)

    _, marker = await marker_for(api, "reply")
    assert marker is None
    inbox = await api.input_runtime_repositories.inbox.list_for_cycle(
        "admitted-cycle"
    )
    assert len(inbox) == 1
    assert inbox[0].state == InboxState.QUEUED


@pytest.mark.asyncio
async def test_waiting_cancellation_after_marker_keeps_claim_evidence(tmp_path):
    api = make_api(
        tmp_path,
        Batch("initial", "session"),
        Batch("reply", "session"),
    )
    continuation = await prepare_waiting(api)
    continuation_mcp = ImmediateMCP()
    api.mcp_client = continuation_mcp
    marker_written = asyncio.Event()
    hold = asyncio.Event()
    original_begin = api.input_admission_service.begin_runtime_handoff

    async def begin_then_hold(*args, **kwargs):
        owns = await original_begin(*args, **kwargs)
        marker_written.set()
        await hold.wait()
        return owns

    api.input_admission_service.begin_runtime_handoff = begin_then_hold
    task = asyncio.create_task(api.resume_admitted_cycle(continuation))
    await marker_written.wait()
    await cancel_task(task)

    _, marker = await marker_for(api, "reply")
    assert marker is not None
    assert marker.state == RuntimeHandoffState.AMBIGUOUS
    inbox = await api.input_runtime_repositories.inbox.list_for_cycle(
        "admitted-cycle"
    )
    assert len(inbox) == 1
    assert inbox[0].state == InboxState.APPLYING
    state = await api.input_runtime_repositories.sessions.get("session")
    assert state is not None
    assert state.cycle_status == CycleStatus.INTERRUPTED

    duplicate = await api.admit_committed_batch("reply", session_id="session")
    assert duplicate.should_wake_runner is False
    assert await api.resume_admitted_cycle(duplicate) is None
    assert continuation_mcp.calls == 0


class InMemoryHandoffRepository:
    def __init__(self):
        self.records = {}

    async def get(self, admission_id):
        return self.records.get(admission_id)

    async def begin(self, candidate):
        return self.records.setdefault(candidate.admission_id, candidate)

    async def complete(
        self,
        admission_id,
        *,
        handoff_token,
        completed_at,
    ):
        current = self.records[admission_id]
        if current.handoff_token != handoff_token:
            raise InputRuntimeConflictError("runtime handoff token mismatch")
        updated = RuntimeHandoffRecord.model_validate(
            current.model_copy(
                update={
                    "state": RuntimeHandoffState.COMPLETED,
                    "completed_at": completed_at,
                }
            ).model_dump(mode="python")
        )
        self.records[admission_id] = updated
        return updated

    async def mark_ambiguous(
        self,
        admission_id,
        *,
        handoff_token,
        ambiguous_at,
        error_code,
    ):
        current = self.records[admission_id]
        if current.handoff_token != handoff_token:
            raise InputRuntimeConflictError("runtime handoff token mismatch")
        updated = RuntimeHandoffRecord.model_validate(
            current.model_copy(
                update={
                    "state": RuntimeHandoffState.AMBIGUOUS,
                    "ambiguous_at": ambiguous_at,
                    "error_code": error_code,
                }
            ).model_dump(mode="python")
        )
        self.records[admission_id] = updated
        return updated


def admission():
    return InputAdmissionRecord(
        session_id="session",
        input_batch_id="batch",
        session_sequence=1,
        target_cycle_id="cycle",
        cycle_sequence=0,
        admitted_generation=0,
        payload_size_bytes=1,
        admission_kind=AdmissionKind.START_CYCLE,
        idempotency_key="key-batch",
        admitted_at=NOW,
    )


def test_runtime_handoff_protocol_has_command_oriented_surface():
    methods = {
        name
        for name, value in inspect.getmembers(
            RuntimeHandoffRepository,
            inspect.isfunction,
        )
        if not name.startswith("_")
    }
    assert methods == {"get", "begin", "complete", "mark_ambiguous"}
    assert getattr(RuntimeHandoffRepository, "_is_protocol", False)
    assert not methods & {"save", "load", "patch", "execute"}


@pytest.mark.asyncio
async def test_service_uses_in_memory_handoff_port_without_root_or_locks(tmp_path):
    bundle = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )
    fake = InMemoryHandoffRepository()
    neutral_bundle = replace(
        bundle,
        handoffs=fake,
        coordination_root=None,
        coordination_locks=None,
    )
    service = InputAdmissionService(
        config=InputRuntimeConfigType(),
        repositories=neutral_bundle,
        committed_batches=Reader(Batch("batch", "session")),
        wake_coordinator=SessionExecutionCoordinator(),
        clock=lambda: NOW,
    )
    record = admission()
    assert await service.begin_runtime_handoff(
        record,
        handoff_token="attempt",
    )
    assert await fake.get(record.admission_id) is not None


@pytest.mark.asyncio
async def test_filesystem_bundle_provides_restart_safe_handoffs(tmp_path):
    first = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )
    assert isinstance(first.handoffs, RuntimeHandoffRepository)
    record = RuntimeHandoffRecord(
        admission_id="adm_" + "1" * 32,
        session_id="session",
        input_batch_id="batch",
        cycle_id="cycle",
        handoff_token="attempt",
        handed_off_at=NOW,
    )
    await first.handoffs.begin(record)

    restarted = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )
    assert await restarted.handoffs.get(record.admission_id) == record


@pytest.mark.asyncio
async def test_stale_handoff_token_cannot_complete_another_attempt(tmp_path):
    bundle = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )
    record = RuntimeHandoffRecord(
        admission_id="adm_" + "2" * 32,
        session_id="session",
        input_batch_id="batch",
        cycle_id="cycle",
        handoff_token="current-attempt",
        handed_off_at=NOW,
    )
    await bundle.handoffs.begin(record)

    with pytest.raises(InputRuntimeConflictError):
        await bundle.handoffs.complete(
            record.admission_id,
            handoff_token="stale-attempt",
            completed_at=NOW + timedelta(seconds=1),
        )
    assert (
        await bundle.handoffs.get(record.admission_id)
    ).state == RuntimeHandoffState.HANDED_OFF


@pytest.mark.parametrize(
    ("state", "field", "value", "error_code"),
    [
        (
            RuntimeHandoffState.COMPLETED,
            "completed_at",
            NOW - timedelta(seconds=1),
            None,
        ),
        (
            RuntimeHandoffState.AMBIGUOUS,
            "ambiguous_at",
            NOW - timedelta(seconds=1),
            "cancelled",
        ),
    ],
)
def test_terminal_handoff_timestamp_cannot_precede_handoff(
    state,
    field,
    value,
    error_code,
):
    payload = {
        "admission_id": "adm_" + "3" * 32,
        "session_id": "session",
        "input_batch_id": "batch",
        "cycle_id": "cycle",
        "handoff_token": "attempt",
        "state": state,
        "handed_off_at": NOW,
        field: value,
        "error_code": error_code,
    }
    with pytest.raises(ValidationError):
        RuntimeHandoffRecord(**payload)
