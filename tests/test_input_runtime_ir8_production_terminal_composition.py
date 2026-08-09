from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.api import input_runtime_recovery as lifecycle
from src.api.input_runtime_recovery_composition import (
    ProductionInputRuntimeRecoveryCoordinator,
    install_production_recovery_types,
)
from src.input_runtime import (
    CheckpointName,
    CycleInputApplier,
    CycleStatus,
    FinalizationState,
    InputAdmissionService,
    InputRuntimeConfigType,
    create_filesystem_input_runtime_repositories,
)
from src.input_runtime.handoff import RuntimeHandoffState
from src.input_runtime.recovery import (
    InputRuntimeLifecycleState,
    InputRuntimeRecoveryError,
)
from src.interaction.output_models import OutputBatchKind
from src.runtime import ActiveAgentCycle, SessionExecutionCoordinator
from src.storage import StorageConfigType

NOW = datetime(2026, 8, 9, 9, 0, tzinfo=timezone.utc)
OUTPUT_ID = "obat_" + "4" * 32


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
    def __init__(self) -> None:
        self.batch = Batch()

    async def get_committed(self, input_batch_id: str):
        if input_batch_id != self.batch.input_batch_id:
            raise KeyError(input_batch_id)
        return self.batch

    async def list_committed_for_recovery(self):
        return (self.batch,)


class OutputStore:
    def __init__(self, output=None) -> None:
        self.output = output
        self.get_calls = 0

    async def get(self, output_batch_id: str):
        self.get_calls += 1
        if self.output is None:
            raise KeyError(output_batch_id)
        return self.output


class FakeMCP:
    def __init__(self) -> None:
        self.connect_calls = 0
        self.tool_calls = 0
        self.delivery_calls = 0

    async def connect_to_servers(self, configs=()):
        self.connect_calls += 1


async def _seed_active_runtime(tmp_path):
    reader = Reader()
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
    outcome = await service.admit_committed_batch("initial", session_id="session")
    admission = outcome.admission
    assert admission is not None
    cycle = ActiveAgentCycle(
        cycle_id=admission.target_cycle_id,
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
        cycle_id=admission.target_cycle_id,
        generation=0,
        checkpoint=CheckpointName.RESUME,
        active_cycle=cycle,
        input_batch_id="initial",
    )
    return reader, repositories, service, admission


async def _seed_terminal_runtime(tmp_path, *, bind_handoff: bool = True):
    reader, repositories, service, admission = await _seed_active_runtime(tmp_path)
    token = "handoff-token"
    if bind_handoff:
        assert await service.begin_runtime_handoff(
            admission,
            handoff_token=token,
        )
    candidate = await service.finalization_service.capture_candidate(
        session_id="session",
        cycle_id=admission.target_cycle_id,
    )
    prepared = await service.finalization_service.prepare(candidate)
    assert prepared.record is not None
    record = await service.finalization_service.persist_result(
        prepared.record.finalization_id,
        {"content": "final", "status": "done"},
    )
    record = await service.finalization_service.mark_output_ready(
        record.finalization_id,
        output_batch_id=OUTPUT_ID,
    )
    record = await service.finalization_service.terminal_commit(record.finalization_id)
    assert record.state == FinalizationState.TERMINAL_COMMITTED
    if bind_handoff:
        marker = await repositories.handoffs.get(admission.admission_id)
        assert marker is not None and marker.state == RuntimeHandoffState.COMPLETED
        # Clear the process-local handoff ContextVar after the durable commit.
        await service.complete_runtime_handoff(admission, handoff_token=token)
    return reader, record


async def _seed_done_projection_without_marker(tmp_path):
    reader, repositories, _, admission = await _seed_active_runtime(tmp_path)
    snapshot = await repositories.snapshots.get(admission.target_cycle_id)
    assert snapshot is not None
    await repositories.snapshots.compare_and_swap(
        snapshot.snapshot_revision,
        snapshot.model_copy(
            update={
                "status": CycleStatus.DONE,
                "snapshot_revision": snapshot.snapshot_revision + 1,
                "updated_at": NOW,
            }
        ),
    )
    state = await repositories.sessions.get("session")
    assert state is not None
    await repositories.sessions.compare_and_swap(
        state.revision,
        state.model_copy(
            update={
                "cycle_status": CycleStatus.DONE,
                "finalization_id": None,
                "revision": state.revision + 1,
                "updated_at": NOW,
            }
        ),
    )
    return reader


def _matching_output():
    return SimpleNamespace(
        output_batch_id=OUTPUT_ID,
        session_id="session",
        cycle_id="cycle",
        kind=OutputBatchKind.FINAL,
    )


def _compose_fresh_recovery(tmp_path, reader, output_store, monkeypatch):
    repositories = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )
    coordinator = SessionExecutionCoordinator()
    service = InputAdmissionService(
        config=InputRuntimeConfigType(),
        repositories=repositories,
        committed_batches=reader,
        wake_coordinator=coordinator,
        cycle_id_factory=lambda: "must-not-create-old-cycle",
        clock=lambda: NOW,
        payload_size_resolver=lambda batch: batch.payload_size,
    )
    api = SimpleNamespace(
        ingress_services=SimpleNamespace(batch_store=object()),
        input_runtime_repositories=repositories,
        input_admission_service=service,
        execution_coordinator=coordinator,
        output_store=output_store,
    )
    monkeypatch.setattr(
        lifecycle,
        "FileSystemCommittedInputBatchRecoveryReader",
        lambda _store: reader,
    )
    install_production_recovery_types()
    lifecycle._ensure_components(api)
    assert type(api.input_runtime_recovery) is ProductionInputRuntimeRecoveryCoordinator
    return api


@pytest.mark.asyncio
async def test_production_composed_done_without_terminal_marker_fails_before_mcp(
    tmp_path,
    monkeypatch,
):
    reader = await _seed_done_projection_without_marker(tmp_path)
    mcp = FakeMCP()
    api = _compose_fresh_recovery(tmp_path, reader, OutputStore(), monkeypatch)

    with pytest.raises(InputRuntimeRecoveryError) as error:
        await api.input_runtime_recovery.recover()
    assert error.value.reason_code == "terminal_session_without_authoritative_marker"
    assert api.input_runtime_readiness_gate.state == InputRuntimeLifecycleState.FAILED
    assert api.input_runtime_readiness_gate.is_ready is False
    assert mcp.connect_calls == 0


@pytest.mark.asyncio
async def test_production_composed_terminal_marker_missing_output_fails_before_mcp(
    tmp_path,
    monkeypatch,
):
    reader, _ = await _seed_terminal_runtime(tmp_path)
    mcp = FakeMCP()
    api = _compose_fresh_recovery(tmp_path, reader, OutputStore(), monkeypatch)

    with pytest.raises(InputRuntimeRecoveryError) as error:
        await api.input_runtime_recovery.recover()
    assert error.value.reason_code == "finalization_output_missing"
    assert api.input_runtime_readiness_gate.state == InputRuntimeLifecycleState.FAILED
    assert api.input_runtime_readiness_gate.is_ready is False
    assert mcp.connect_calls == 0


@pytest.mark.asyncio
async def test_production_composed_terminal_without_completed_handoff_is_rejected(
    tmp_path,
    monkeypatch,
):
    reader, _ = await _seed_terminal_runtime(tmp_path, bind_handoff=False)
    api = _compose_fresh_recovery(
        tmp_path,
        reader,
        OutputStore(_matching_output()),
        monkeypatch,
    )

    with pytest.raises(InputRuntimeRecoveryError) as error:
        await api.input_runtime_recovery.recover()
    assert error.value.reason_code == "terminal_handoff_not_completed"
    assert api.input_runtime_readiness_gate.state == InputRuntimeLifecycleState.FAILED


@pytest.mark.asyncio
async def test_production_composed_valid_terminal_restart_is_idempotent_and_local(
    tmp_path,
    monkeypatch,
):
    reader, terminal_before = await _seed_terminal_runtime(tmp_path)
    output_store = OutputStore(_matching_output())
    api = _compose_fresh_recovery(tmp_path, reader, output_store, monkeypatch)
    mcp = FakeMCP()

    plan = await api.input_runtime_recovery.recover()
    assert plan.sessions == ()
    assert api.input_runtime_readiness_gate.state == InputRuntimeLifecycleState.RECOVERING
    assert api.input_runtime_readiness_gate.is_ready is False
    assert mcp.connect_calls == 0
    assert mcp.tool_calls == 0
    assert mcp.delivery_calls == 0

    # Production may connect MCP only after deterministic recovery has returned.
    await mcp.connect_to_servers(())
    assert mcp.connect_calls == 1

    terminal_after = await api.input_runtime_repositories.finalizations.get(
        terminal_before.finalization_id
    )
    assert terminal_after == terminal_before
    assert output_store.get_calls >= 1
