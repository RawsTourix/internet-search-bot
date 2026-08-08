from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pytest

import src.api.api as api_module
from src.input_runtime import (
    ActiveCycleSnapshot,
    CheckpointName,
    CycleStatus,
    FinalizationState,
    InputAdmissionService,
    InputRuntimeConfigType,
    SessionInputRuntimeState,
    create_filesystem_input_runtime_repositories,
    new_context_revision_id,
)
from src.input_runtime.finalization import FinalizationBarrierService
from src.mcp.artifact_delivery_runtime import FinalizingArtifactDeliveryPlanningMCPClient
from src.mcp.input_runtime_checkpoint_hardening import InputRuntimeCheckpointHardeningMixin
from src.mcp.input_runtime_checkpoints import InputRuntimeCheckpointMixin
from src.mcp.input_runtime_controls import InputRuntimeControlMixin
from src.runtime import ActiveAgentCycle
from src.storage import StorageConfigType

NOW = datetime(2026, 8, 8, 12, 30, tzinfo=timezone.utc)


def repos(tmp_path):
    return create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )


async def seed_finalization(tmp_path):
    repositories = repos(tmp_path)
    context_id = new_context_revision_id()
    await repositories.sessions.create_if_absent(
        SessionInputRuntimeState(
            session_id="session",
            generation=0,
            active_cycle_id="cycle",
            cycle_status=CycleStatus.RUNNING,
            active_context_revision_id=context_id,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    await repositories.snapshots.create_if_absent(
        ActiveCycleSnapshot(
            cycle_id="cycle",
            session_id="session",
            generation=0,
            status=CycleStatus.RUNNING,
            original_input_batch_id="ibat_" + "1" * 32,
            original_user_request="initial",
            messages_for_llm=[{"role": "user", "content": "initial"}],
            cycle_trace=[],
            applied_input_batch_ids=["ibat_" + "1" * 32],
            applied_through_cycle_sequence=0,
            active_context_revision_id=context_id,
            safe_checkpoint=CheckpointName.BEFORE_LLM,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    service = FinalizationBarrierService(repositories=repositories, clock=lambda: NOW)
    candidate = await service.capture_candidate(session_id="session", cycle_id="cycle")
    record = (await service.prepare(candidate)).record
    record = await service.persist_result(record.finalization_id, {"content": "final"})
    record = await service.mark_output_ready(
        record.finalization_id,
        output_batch_id="obat_" + "2" * 32,
    )
    return repositories, service, record


def test_production_api_uses_ir7_checkpoint_mro():
    production = FinalizingArtifactDeliveryPlanningMCPClient
    assert api_module.FinalizingArtifactDeliveryPlanningMCPClient is production
    mro = production.mro()
    assert mro.index(InputRuntimeControlMixin) < mro.index(InputRuntimeCheckpointHardeningMixin)
    assert mro.index(InputRuntimeCheckpointHardeningMixin) < mro.index(InputRuntimeCheckpointMixin)
    assert production.process_query is InputRuntimeControlMixin.process_query
    assert production._complete_finalization is InputRuntimeCheckpointMixin._complete_finalization
    assert production._run_input_checkpoint is InputRuntimeCheckpointHardeningMixin._run_input_checkpoint


@pytest.mark.asyncio
async def test_partial_terminal_snapshot_then_late_input_converges_to_abort(tmp_path, monkeypatch):
    repositories, service, record = await seed_finalization(tmp_path)
    import src.input_runtime.ir7_filesystem as adapter

    original = adapter.atomic_write_model
    failed = False

    def injected(path, model):
        nonlocal failed
        if (
            not failed
            and isinstance(model, SessionInputRuntimeState)
            and model.cycle_status == CycleStatus.DONE
        ):
            failed = True
            raise RuntimeError("session terminal write failed")
        return original(path, model)

    monkeypatch.setattr(adapter, "atomic_write_model", injected)
    with pytest.raises(RuntimeError, match="session terminal write failed"):
        await service.terminal_commit(record.finalization_id)
    monkeypatch.setattr(adapter, "atomic_write_model", original)

    partial_snapshot = await repositories.snapshots.get("cycle")
    partial_state = await repositories.sessions.get("session")
    assert partial_snapshot.status == CycleStatus.DONE
    assert partial_state.cycle_status == CycleStatus.FINALIZING

    late = partial_state.model_copy(
        update={
            "active_cycle_accepted_through_sequence": 1,
            "revision": partial_state.revision + 1,
            "updated_at": NOW + timedelta(seconds=1),
        }
    )
    await repositories.sessions.compare_and_swap(partial_state.revision, late)

    recreated = repos(tmp_path)
    replay = FinalizationBarrierService(
        repositories=recreated,
        clock=lambda: NOW + timedelta(seconds=2),
    )
    aborted = await replay.terminal_commit(record.finalization_id)
    assert aborted.state == FinalizationState.ABORTED_NEW_INPUT
    snapshot = await recreated.snapshots.get("cycle")
    state = await recreated.sessions.get("session")
    assert snapshot.status == CycleStatus.RUNNING
    assert state.cycle_status == CycleStatus.RUNNING
    assert state.finalization_id is None
    assert (state.active_cycle_accepted_through_sequence, state.active_cycle_applied_through_sequence) == (1, 0)


@dataclass
class Batch:
    input_batch_id: str
    session_id: str = "session"
    payload_size: int = 10
    text_parts: list[object] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    source_event_ids: tuple[str, ...] = ("evt_" + "1" * 32,)
    content_fingerprint: str = "sha256:" + "2" * 64
    committed_at: datetime = NOW
    continuation_of_batch_id: str | None = None
    correction_of_batch_id: str | None = None
    artifact_manifest: object = field(default_factory=lambda: type("Manifest", (), {"items": ()})())

    def model_dump_json(self) -> str:
        return "x" * self.payload_size


class Reader:
    def __init__(self, *batches: Batch) -> None:
        self.batches = {batch.input_batch_id: batch for batch in batches}

    async def get_committed(self, input_batch_id: str):
        return self.batches[input_batch_id]


class Wake:
    async def wake(self, session_id: str, *, cycle_id: str) -> bool:
        return True


@pytest.mark.asyncio
async def test_post_terminal_input_is_admitted_as_new_cycle(tmp_path):
    repositories = repos(tmp_path)
    ids = iter(("cycle-a", "cycle-b"))
    service = InputAdmissionService(
        config=InputRuntimeConfigType(),
        repositories=repositories,
        committed_batches=Reader(Batch("initial"), Batch("next")),
        wake_coordinator=Wake(),
        cycle_id_factory=lambda: next(ids),
        clock=lambda: NOW,
        payload_size_resolver=lambda batch: batch.payload_size,
    )
    initial = await service.admit_committed_batch("initial", session_id="session")
    active = ActiveAgentCycle(
        cycle_id=initial.target_cycle_id,
        session_id="session",
        original_user_request="initial",
        messages_for_llm=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": '{"type":"user_request"}'},
        ],
        cycle_trace=[],
        original_user_message_index=1,
        original_input_batch_id="initial",
        input_runtime_generation=0,
    )
    await service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.RESUME,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    candidate = await service.finalization_service.capture_candidate(
        session_id="session", cycle_id="cycle-a"
    )
    record = (await service.finalization_service.prepare(candidate)).record
    record = await service.finalization_service.persist_result(
        record.finalization_id, {"content": "final"}
    )
    record = await service.finalization_service.mark_output_ready(
        record.finalization_id, output_batch_id="obat_" + "3" * 32
    )
    assert (
        await service.finalization_service.terminal_commit(record.finalization_id)
    ).state == FinalizationState.TERMINAL_COMMITTED

    outcome = await service.admit_committed_batch("next", session_id="session")
    assert outcome.target_cycle_id == "cycle-b"
    assert outcome.admitted_generation == 0
    state = await repositories.sessions.get("session")
    assert state.active_cycle_id == "cycle-b"
    assert state.cycle_status == CycleStatus.RUNNING
