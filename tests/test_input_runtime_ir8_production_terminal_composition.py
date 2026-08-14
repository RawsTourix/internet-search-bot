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
from src.ingress.models import ClientResponseRoute
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
from src.input_runtime.handoff_context import clear_runtime_handoff_context_for_tests
from src.input_runtime.recovery import (
    InputRuntimeLifecycleState,
    InputRuntimeRecoveryError,
)
from src.interaction.capabilities import ClientCapabilitySnapshot
from src.interaction.ids import new_capability_snapshot_id, new_output_part_id
from src.interaction.output_models import (
    OutputBatch,
    OutputBatchKind,
    OutputBatchState,
    TextOutputPart,
)
from src.interaction.output_store import FileSystemOutputBatchStore
from src.runtime import ActiveAgentCycle, SessionExecutionCoordinator
from src.storage import StorageConfigType

NOW = datetime(2026, 8, 9, 9, 0, tzinfo=timezone.utc)
INPUT_ID = "ibat_" + "1" * 32
OUTPUT_ID = "obat_" + "4" * 32


@dataclass
class Batch:
    input_batch_id: str = INPUT_ID
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


class FakeMCP:
    def __init__(self) -> None:
        self.connect_calls = 0
        self.llm_calls = 0
        self.tool_calls = 0
        self.delivery_calls = 0

    async def connect_to_servers(self, configs=()):
        self.connect_calls += 1


async def _seed_active_runtime(tmp_path):
    clear_runtime_handoff_context_for_tests()
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
    outcome = await service.admit_committed_batch(INPUT_ID, session_id="session")
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
        original_input_batch_id=INPUT_ID,
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
        input_batch_id=INPUT_ID,
    )
    return reader, repositories, service, admission


async def _prepare_bound_output_ready(tmp_path):
    reader, repositories, service, admission = await _seed_active_runtime(tmp_path)
    token = "handoff-token"
    assert await service.begin_runtime_handoff(admission, handoff_token=token)
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
    assert record.state == FinalizationState.OUTPUT_READY
    return reader, repositories, service, admission, token, record


async def _seed_terminal_runtime(tmp_path):
    (
        reader,
        repositories,
        service,
        admission,
        token,
        record,
    ) = await _prepare_bound_output_ready(tmp_path)
    record = await service.finalization_service.terminal_commit(record.finalization_id)
    assert record.state == FinalizationState.TERMINAL_COMMITTED
    marker = await repositories.handoffs.get(admission.admission_id)
    assert marker is not None and marker.state == RuntimeHandoffState.COMPLETED
    await service.complete_runtime_handoff(admission, handoff_token=token)
    return reader, record


async def _seed_terminal_marker_with_open_handoff(tmp_path):
    (
        reader,
        repositories,
        _,
        admission,
        _,
        record,
    ) = await _prepare_bound_output_ready(tmp_path)
    marker = await repositories.handoffs.get(admission.admission_id)
    assert marker is not None and marker.state == RuntimeHandoffState.HANDED_OFF

    terminal = record.model_copy(
        update={
            "state": FinalizationState.TERMINAL_COMMITTED,
            "updated_at": NOW,
        }
    )
    terminal = await repositories.finalizations.advance(
        record.finalization_id,
        expected_state=FinalizationState.OUTPUT_READY.value,
        next_record=terminal,
    )
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
                "finalization_id": terminal.finalization_id,
                "revision": state.revision + 1,
                "updated_at": NOW,
            }
        ),
    )
    clear_runtime_handoff_context_for_tests()
    marker = await repositories.handoffs.get(admission.admission_id)
    assert marker is not None and marker.state == RuntimeHandoffState.HANDED_OFF
    return reader, terminal


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


def _matching_output() -> OutputBatch:
    capability = ClientCapabilitySnapshot(
        capability_snapshot_id=new_capability_snapshot_id(),
        capability_contract_version=1,
        client_type="telegram",
        client_instance_id="bot-a",
        features=(),
        limits={},
        fingerprint="sha256:" + "a" * 64,
        captured_at=NOW,
    )
    return OutputBatch(
        output_batch_id=OUTPUT_ID,
        input_batch_id=INPUT_ID,
        session_id="session",
        cycle_id="cycle",
        sequence_number=1,
        kind=OutputBatchKind.FINAL,
        response_route=ClientResponseRoute(
            route_type="telegram",
            conversation_id="100",
        ),
        locale="en",
        capability_snapshot=capability,
        parts=(
            TextOutputPart(
                part_id=new_output_part_id(),
                index=0,
                text="final",
            ),
        ),
        state=OutputBatchState.READY,
        created_at=NOW,
        ready_at=NOW,
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
    mcp = FakeMCP()
    api = SimpleNamespace(
        ingress_services=SimpleNamespace(batch_store=object()),
        input_runtime_repositories=repositories,
        input_admission_service=service,
        execution_coordinator=coordinator,
        output_store=output_store,
        mcp_client=mcp,
    )
    monkeypatch.setattr(
        lifecycle,
        "FileSystemCommittedInputBatchRecoveryReader",
        lambda _store: reader,
    )
    install_production_recovery_types()
    lifecycle._ensure_components(api)
    api.input_runtime_recovery.clock = lambda: NOW
    assert type(api.input_runtime_recovery) is ProductionInputRuntimeRecoveryCoordinator
    return api


@pytest.mark.asyncio
async def test_production_composed_done_without_terminal_marker_fails_before_mcp(
    tmp_path,
    monkeypatch,
):
    reader = await _seed_done_projection_without_marker(tmp_path)
    output_store = FileSystemOutputBatchStore(tmp_path)
    api = _compose_fresh_recovery(tmp_path, reader, output_store, monkeypatch)

    with pytest.raises(InputRuntimeRecoveryError) as error:
        await api.input_runtime_recovery.recover()
    assert error.value.reason_code == "terminal_session_without_authoritative_marker"
    assert api.input_runtime_readiness_gate.state == InputRuntimeLifecycleState.FAILED
    assert api.input_runtime_readiness_gate.is_ready is False
    assert api.mcp_client.connect_calls == 0


@pytest.mark.asyncio
async def test_production_composed_terminal_marker_missing_output_fails_before_mcp(
    tmp_path,
    monkeypatch,
):
    reader, _ = await _seed_terminal_runtime(tmp_path)
    output_store = FileSystemOutputBatchStore(tmp_path)
    api = _compose_fresh_recovery(tmp_path, reader, output_store, monkeypatch)

    with pytest.raises(InputRuntimeRecoveryError) as error:
        await api.input_runtime_recovery.recover()
    assert error.value.reason_code == "finalization_output_missing"
    assert api.input_runtime_readiness_gate.state == InputRuntimeLifecycleState.FAILED
    assert api.input_runtime_readiness_gate.is_ready is False
    assert api.mcp_client.connect_calls == 0


@pytest.mark.asyncio
async def test_production_composed_terminal_with_open_handoff_is_rejected(
    tmp_path,
    monkeypatch,
):
    reader, _ = await _seed_terminal_marker_with_open_handoff(tmp_path)
    output_store = FileSystemOutputBatchStore(tmp_path)
    await output_store.commit(_matching_output())
    api = _compose_fresh_recovery(
        tmp_path,
        reader,
        output_store,
        monkeypatch,
    )

    with pytest.raises(InputRuntimeRecoveryError) as error:
        await api.input_runtime_recovery.recover()
    assert error.value.reason_code == "terminal_handoff_not_completed"
    assert api.input_runtime_readiness_gate.state == InputRuntimeLifecycleState.FAILED
    assert api.mcp_client.connect_calls == 0


@pytest.mark.asyncio
async def test_production_composed_valid_terminal_restart_is_idempotent_and_local(
    tmp_path,
    monkeypatch,
):
    reader, terminal_before = await _seed_terminal_runtime(tmp_path)
    output_store = FileSystemOutputBatchStore(tmp_path)
    output_before, created = await output_store.commit(_matching_output())
    assert created is True
    api = _compose_fresh_recovery(tmp_path, reader, output_store, monkeypatch)

    plan = await api.input_runtime_recovery.recover()
    assert plan.sessions == ()
    assert api.input_runtime_readiness_gate.state == InputRuntimeLifecycleState.RECOVERING
    assert api.input_runtime_readiness_gate.is_ready is False
    assert api.mcp_client.connect_calls == 0
    assert api.mcp_client.llm_calls == 0
    assert api.mcp_client.tool_calls == 0
    assert api.mcp_client.delivery_calls == 0

    await api.mcp_client.connect_to_servers(())
    assert api.mcp_client.connect_calls == 1

    terminal_after = await api.input_runtime_repositories.finalizations.get(
        terminal_before.finalization_id
    )
    output_after = await output_store.get(OUTPUT_ID)
    assert terminal_after == terminal_before
    assert output_after == output_before
