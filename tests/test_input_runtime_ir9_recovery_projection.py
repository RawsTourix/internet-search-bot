from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.input_runtime import (
    CheckpointAction,
    CheckpointName,
    CycleStatus,
    InputAdmissionService,
    InputRuntimeConfigType,
    create_filesystem_input_runtime_repositories,
)
from src.input_runtime.diagnostics import (
    InputRuntimeDiagnosticsService,
    RuntimeProcessStatus,
    RuntimeRecoveryNotice,
)
from src.input_runtime.ir9_filesystem import FileSystemRuntimeDiagnosticsReader
from src.input_runtime.recovery import InputRuntimeReadinessGate
from src.input_runtime.recovery_hardening import InputRuntimeRecoveryCoordinator
from src.runtime import ActiveAgentCycle, SessionExecutionCoordinator
from src.storage import StorageConfigType


NOW = datetime(2026, 8, 9, 15, 0, tzinfo=timezone.utc)


@dataclass
class Batch:
    input_batch_id: str
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


class Wake:
    async def wake(self, session_id: str, *, cycle_id: str) -> bool:
        return True


def active_cycle(cycle_id: str) -> ActiveAgentCycle:
    return ActiveAgentCycle(
        cycle_id=cycle_id,
        session_id="session",
        original_user_request="initial",
        messages_for_llm=[
            {"role": "system", "content": "system"},
            {"role": "user", "content": "initial"},
        ],
        cycle_trace=[],
        original_user_message_index=1,
        original_input_batch_id="initial",
        input_runtime_generation=0,
    )


@pytest.mark.asyncio
async def test_ir9_fresh_process_projects_recovered_paused_durable_state(tmp_path):
    batch = Batch("initial")
    initial_repositories = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )
    initial_service = InputAdmissionService(
        config=InputRuntimeConfigType(),
        repositories=initial_repositories,
        committed_batches=Reader(batch),
        wake_coordinator=Wake(),
        cycle_id_factory=lambda: "cycle-a",
        clock=lambda: NOW,
        payload_size_resolver=lambda item: item.payload_size,
    )
    admitted = await initial_service.admit_committed_batch(
        "initial",
        session_id="session",
    )
    active = active_cycle(admitted.target_cycle_id)
    await initial_service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.RESUME,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    await initial_service.control_service.request_pause(
        session_id="session",
        idempotency_key="pause-before-restart",
        source_client_type="test",
        source_message_ref={"message_id": 1},
    )
    pause_checkpoint = await initial_service.checkpoint_service.run_checkpoint(
        checkpoint=CheckpointName.BEFORE_LLM,
        active_cycle=active,
        desired_status=CycleStatus.RUNNING,
    )
    assert pause_checkpoint.action == CheckpointAction.PAUSE

    # Fresh process: no reuse of in-memory service/coordinator state.
    fresh_repositories = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )
    reader = Reader(batch)
    coordinator = SessionExecutionCoordinator()
    fresh_service = InputAdmissionService(
        config=InputRuntimeConfigType(),
        repositories=fresh_repositories,
        committed_batches=reader,
        wake_coordinator=coordinator,
        cycle_id_factory=lambda: "must-not-be-used",
        clock=lambda: NOW,
        payload_size_resolver=lambda item: item.payload_size,
    )
    gate = InputRuntimeReadinessGate()
    recovery = InputRuntimeRecoveryCoordinator(
        repositories=fresh_repositories,
        admission_service=fresh_service,
        committed_batches=reader,
        readiness_gate=gate,
        generation_coordinator=coordinator,
        clock=lambda: NOW,
    )
    plan = await recovery.recover()
    recovered = next(item for item in plan.sessions if item.session_id == "session")
    gate.mark_ready()

    diagnostics = InputRuntimeDiagnosticsService(
        FileSystemRuntimeDiagnosticsReader(
            root=fresh_repositories.coordination_root,
            locks=fresh_repositories.coordination_locks,
        ),
        clock=lambda: NOW,
        process_status_provider=lambda: RuntimeProcessStatus(
            state=gate.state.value,
            failure_reason_code=gate.failure_reason,
        ),
        recovery_notice_provider=lambda session_id: RuntimeRecoveryNotice(
            disposition=recovered.disposition.value,
            reason_code=recovered.reason_code,
            automatic_replay_enabled=True,
        )
        if session_id == "session"
        else None,
    )
    status = await diagnostics.status("session")

    assert status.process_readiness == "ready"
    assert status.session_exists is True
    assert status.session_status == CycleStatus.PAUSED_BY_USER
    assert status.paused is True
    assert status.active_cycle_id == "cycle-a"
    assert status.controls.applied_sequence == 1
    assert status.recovery_notice is not None
    assert status.recovery_notice.disposition == recovered.disposition.value
