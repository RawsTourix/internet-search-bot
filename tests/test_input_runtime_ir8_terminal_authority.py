from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.input_runtime import (
    CheckpointName,
    CycleInputApplier,
    CycleStatus,
    InputAdmissionService,
    InputRuntimeConfigType,
    create_filesystem_input_runtime_repositories,
)
from src.input_runtime.recovery import (
    InputRuntimeLifecycleState,
    InputRuntimeReadinessGate,
    InputRuntimeRecoveryError,
)
from src.input_runtime.recovery_terminal import InputRuntimeRecoveryCoordinator
from src.runtime import ActiveAgentCycle, SessionExecutionCoordinator
from src.storage import StorageConfigType

NOW = datetime(2026, 8, 8, 21, 30, tzinfo=timezone.utc)


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
        if input_batch_id != "initial":
            raise KeyError(input_batch_id)
        return self.batch

    async def list_committed_for_recovery(self):
        return (self.batch,)


@pytest.mark.asyncio
async def test_done_projection_without_terminal_committed_is_fatal(tmp_path):
    reader = Reader()
    repositories = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )
    service = InputAdmissionService(
        config=InputRuntimeConfigType(),
        repositories=repositories,
        committed_batches=reader,
        wake_coordinator=SessionExecutionCoordinator(),
        cycle_id_factory=lambda: "cycle",
        clock=lambda: NOW,
        payload_size_resolver=lambda batch: batch.payload_size,
    )
    admitted = await service.admit_committed_batch("initial", session_id="session")
    cycle = ActiveAgentCycle(
        cycle_id=admitted.target_cycle_id,
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
        cycle_id=admitted.target_cycle_id,
        generation=0,
        checkpoint=CheckpointName.RESUME,
        active_cycle=cycle,
        input_batch_id="initial",
    )

    snapshot = await repositories.snapshots.get(admitted.target_cycle_id)
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

    fresh_repositories = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )
    fresh_coordinator = SessionExecutionCoordinator()
    fresh_service = InputAdmissionService(
        config=InputRuntimeConfigType(),
        repositories=fresh_repositories,
        committed_batches=reader,
        wake_coordinator=fresh_coordinator,
        cycle_id_factory=lambda: "must-not-create",
        clock=lambda: NOW,
        payload_size_resolver=lambda batch: batch.payload_size,
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

    with pytest.raises(InputRuntimeRecoveryError) as error:
        await recovery.recover()
    assert error.value.reason_code == "terminal_session_without_authoritative_marker"
    assert gate.state == InputRuntimeLifecycleState.FAILED
