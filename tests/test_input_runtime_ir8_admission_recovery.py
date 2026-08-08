from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.input_runtime import (
    InputAdmissionService,
    InputRuntimeConfigType,
    create_filesystem_input_runtime_repositories,
)
from src.input_runtime.recovery import (
    InputRuntimeLifecycleState,
    InputRuntimeReadinessGate,
    InputRuntimeRecoveryError,
)
from src.input_runtime.recovery_hardening import InputRuntimeRecoveryCoordinator
from src.runtime import SessionExecutionCoordinator
from src.storage import StorageConfigType

NOW = datetime(2026, 8, 8, 19, 45, tzinfo=timezone.utc)


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


def make_service(tmp_path, reader, *, cycle_factory=lambda: "cycle"):
    repositories = create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )
    coordinator = SessionExecutionCoordinator()
    service = InputAdmissionService(
        config=InputRuntimeConfigType(),
        repositories=repositories,
        committed_batches=reader,
        wake_coordinator=coordinator,
        cycle_id_factory=cycle_factory,
        clock=lambda: NOW,
        payload_size_resolver=lambda batch: batch.payload_size,
    )
    return repositories, coordinator, service


async def recover(tmp_path, reader):
    repositories, coordinator, service = make_service(
        tmp_path,
        reader,
        cycle_factory=lambda: "must-not-create",
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
    return repositories, gate, recovery


@pytest.mark.asyncio
async def test_existing_admission_with_missing_inbox_repairs_same_relation(
    tmp_path,
    monkeypatch,
):
    reader = Reader(Batch("initial", 1), Batch("addition", 2))
    repositories, _, service = make_service(tmp_path, reader)
    initial = await service.admit_committed_batch("initial", session_id="session")

    original_ensure = service._ensure_inbox

    async def crash_before_inbox(*args, **kwargs):
        return None

    monkeypatch.setattr(service, "_ensure_inbox", crash_before_inbox)
    addition = await service.admit_committed_batch("addition", session_id="session")
    monkeypatch.setattr(service, "_ensure_inbox", original_ensure)
    assert addition.admission is not None
    assert await repositories.inbox.list_for_cycle(initial.target_cycle_id) == ()

    fresh, _, recovery = await recover(tmp_path, reader)
    await recovery.recover()
    repaired = await fresh.admissions.get_by_input_batch_id("addition")
    items = await fresh.inbox.list_for_cycle(initial.target_cycle_id)
    assert repaired.admission_id == addition.admission.admission_id
    assert repaired.session_sequence == addition.admission.session_sequence
    assert repaired.cycle_sequence == addition.admission.cycle_sequence
    assert len(items) == 1
    assert items[0].admission_id == repaired.admission_id
    assert items[0].input_batch_id == "addition"
    assert items[0].cycle_sequence == 1


@pytest.mark.asyncio
async def test_session_watermark_lag_repairs_from_durable_admissions(tmp_path):
    reader = Reader(Batch("initial", 1), Batch("addition", 2))
    repositories, _, service = make_service(tmp_path, reader)
    await service.admit_committed_batch("initial", session_id="session")
    await service.admit_committed_batch("addition", session_id="session")
    state = await repositories.sessions.get("session")
    lagging = state.model_copy(
        update={
            "accepted_through_session_sequence": 1,
            "active_cycle_accepted_through_sequence": 0,
            "revision": state.revision + 1,
            "updated_at": NOW,
        }
    )
    await repositories.sessions.compare_and_swap(state.revision, lagging)

    fresh, _, recovery = await recover(tmp_path, reader)
    await recovery.recover()
    repaired = await fresh.sessions.get("session")
    assert repaired.accepted_through_session_sequence == 2
    assert repaired.active_cycle_accepted_through_sequence == 1


@pytest.mark.asyncio
async def test_session_watermark_ahead_of_durable_history_fails_startup(tmp_path):
    reader = Reader(Batch("initial", 1), Batch("addition", 2))
    repositories, _, service = make_service(tmp_path, reader)
    await service.admit_committed_batch("initial", session_id="session")
    await service.admit_committed_batch("addition", session_id="session")
    state = await repositories.sessions.get("session")
    impossible = state.model_copy(
        update={
            "accepted_through_session_sequence": 3,
            "active_cycle_accepted_through_sequence": 2,
            "revision": state.revision + 1,
            "updated_at": NOW,
        }
    )
    await repositories.sessions.compare_and_swap(state.revision, impossible)

    _, gate, recovery = await recover(tmp_path, reader)
    with pytest.raises(InputRuntimeRecoveryError):
        await recovery.recover()
    assert gate.state == InputRuntimeLifecycleState.FAILED
