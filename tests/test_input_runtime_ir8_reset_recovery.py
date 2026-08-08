from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.input_runtime import (
    ActiveAgentCycle,
    CheckpointName,
    CycleInputApplier,
    CycleStatus,
    InputAdmissionService,
    InputRuntimeConfigType,
    create_filesystem_input_runtime_repositories,
)
from src.input_runtime.recovery import InputRuntimeReadinessGate
from src.input_runtime.recovery_hardening import InputRuntimeRecoveryCoordinator
from src.runtime import SessionExecutionCoordinator
from src.storage import StorageConfigType

NOW = datetime(2026, 8, 8, 20, 30, tzinfo=timezone.utc)


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


@pytest.mark.asyncio
async def test_generation_advanced_partial_reset_finishes_without_second_increment(
    tmp_path,
    monkeypatch,
):
    reader = Reader(Batch("initial", 1), Batch("addition", 2))
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
    addition = await service.admit_committed_batch(
        "addition",
        session_id="session",
    )
    assert addition.target_cycle_id == initial.target_cycle_id

    original_cancel = repositories.snapshots.cancel_generation

    async def crash_after_generation_advance(*args, **kwargs):
        raise OSError("crash during reset cleanup")

    monkeypatch.setattr(
        repositories.snapshots,
        "cancel_generation",
        crash_after_generation_advance,
    )
    with pytest.raises(OSError, match="crash during reset cleanup"):
        await service.control_service.request_reset(
            session_id="session",
            idempotency_key="reset-1",
            source_client_type="test",
            reason="reset",
        )
    monkeypatch.setattr(repositories.snapshots, "cancel_generation", original_cancel)

    after_crash = await repositories.sessions.get("session")
    assert after_crash.generation == 1
    assert after_crash.active_cycle_id is None
    old_snapshot = await repositories.snapshots.get(initial.target_cycle_id)
    assert old_snapshot.status == CycleStatus.RUNNING

    # Fresh process: new bundle, service and coordinator, same durable root.
    fresh_repositories, fresh_coordinator, fresh_service = make_runtime(
        tmp_path,
        reader,
    )
    gate = InputRuntimeReadinessGate()
    recovery = InputRuntimeRecoveryCoordinator(
        repositories=fresh_repositories,
        admission_service=fresh_service,
        committed_batches=reader,
        readiness_gate=gate,
        generation_coordinator=fresh_coordinator,
        clock=lambda: NOW,
    )
    plan = await recovery.recover()

    state = await fresh_repositories.sessions.get("session")
    assert state.generation == 1
    assert state.active_cycle_id is None
    assert state.cycle_status == CycleStatus.IDLE
    snapshot = await fresh_repositories.snapshots.get(initial.target_cycle_id)
    assert snapshot.status == CycleStatus.CANCELLED
    admissions = await fresh_repositories.admissions.list_for_session("session")
    assert all(item.state.value == "cancelled" for item in admissions)
    inbox = await fresh_repositories.inbox.list_for_cycle(initial.target_cycle_id)
    assert all(item.state.value == "cancelled" for item in inbox)
    controls = await fresh_repositories.controls.list_for_session("session")
    reset = next(item for item in controls if item.idempotency_key == "reset-1")
    assert reset.state.value == "applied"
    assert reset.generation == 0
    process_state = await fresh_coordinator.snapshot("session")
    assert process_state.generation == 1
    assert process_state.active_cycle_id is None
    assert plan.sessions == ()
    assert gate.is_ready is False
