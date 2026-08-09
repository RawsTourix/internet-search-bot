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
    SessionInputRuntimeState,
    clear_runtime_handoff_context_for_tests,
    create_filesystem_input_runtime_repositories,
    new_admission_id,
)
from src.input_runtime._filesystem_common import _Layout
from src.input_runtime.recovery import (
    InputRuntimeLifecycleState,
    InputRuntimeReadinessGate,
    InputRuntimeRecoveryError,
)
from src.input_runtime.recovery_terminal import InputRuntimeRecoveryCoordinator
from src.input_runtime.serialization import atomic_write_model
from src.runtime import ActiveAgentCycle, SessionExecutionCoordinator
from src.storage import StorageConfigType

NOW = datetime(2026, 8, 8, 19, 30, tzinfo=timezone.utc)


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


def bundle(tmp_path):
    return create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )


def service(tmp_path, reader, *, cycle_factory=lambda: "cycle"):
    repositories = bundle(tmp_path)
    coordinator = SessionExecutionCoordinator()
    runtime = InputAdmissionService(
        config=InputRuntimeConfigType(),
        repositories=repositories,
        committed_batches=reader,
        wake_coordinator=coordinator,
        cycle_id_factory=cycle_factory,
        clock=lambda: NOW,
        payload_size_resolver=lambda batch: batch.payload_size,
    )
    return repositories, coordinator, runtime


async def seed_running(tmp_path, reader):
    repositories, _, runtime = service(tmp_path, reader)
    outcome = await runtime.admit_committed_batch("initial", session_id="session")
    cycle = ActiveAgentCycle(
        cycle_id=outcome.target_cycle_id,
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
        config=runtime.config,
        repositories=repositories,
        committed_batches=reader,
        clock=lambda: NOW,
    )
    await applier.ensure_initial_context(
        session_id="session",
        cycle_id=outcome.target_cycle_id,
        generation=0,
        checkpoint=CheckpointName.RESUME,
        active_cycle=cycle,
        input_batch_id="initial",
    )
    return repositories, runtime, outcome


async def recover(tmp_path, reader):
    repositories, coordinator, runtime = service(
        tmp_path,
        reader,
        cycle_factory=lambda: "must-not-create",
    )
    gate = InputRuntimeReadinessGate()
    recovery = InputRuntimeRecoveryCoordinator(
        repositories=repositories,
        admission_service=runtime,
        committed_batches=reader,
        readiness_gate=gate,
        generation_coordinator=coordinator,
        clock=lambda: NOW,
    )
    return repositories, gate, recovery


@pytest.fixture(autouse=True)
def _clear_handoff_context():
    clear_runtime_handoff_context_for_tests()
    yield
    clear_runtime_handoff_context_for_tests()


@pytest.mark.asyncio
async def test_missing_cycle_authority_pointer_is_rebuilt_from_agreeing_records(tmp_path):
    reader = Reader(Batch("initial", 1))
    _, _, outcome = await seed_running(tmp_path, reader)
    pointer = _Layout(tmp_path).cycle_authority(outcome.target_cycle_id)
    assert pointer.exists()
    pointer.unlink()

    _, gate, recovery = await recover(tmp_path, reader)
    await recovery.recover()
    assert pointer.exists()
    assert gate.state == InputRuntimeLifecycleState.RECOVERING


@pytest.mark.asyncio
async def test_dangling_cycle_authority_pointer_is_rebuilt_from_agreeing_records(tmp_path):
    reader = Reader(Batch("initial", 1))
    _, _, outcome = await seed_running(tmp_path, reader)
    pointer = _Layout(tmp_path).cycle_authority(outcome.target_cycle_id)
    payload = json.loads(pointer.read_text(encoding="utf-8"))
    payload["relative_path"] = "cycles/not-the-authoritative-cycle"
    pointer.write_text(json.dumps(payload), encoding="utf-8")

    await (await recover(tmp_path, reader))[2].recover()
    repaired = json.loads(pointer.read_text(encoding="utf-8"))
    assert repaired["relative_path"] != "cycles/not-the-authoritative-cycle"
    assert repaired["session_id"] == "session"
    assert repaired["cycle_id"] == outcome.target_cycle_id


@pytest.mark.asyncio
async def test_divergent_durable_cycle_session_authority_is_rejected(tmp_path):
    reader = Reader(Batch("initial", 1), Batch("other-input", 1, session_id="other"))
    repositories, _, outcome = await seed_running(tmp_path, reader)
    original = await repositories.admissions.get_by_input_batch_id("initial")
    state = await repositories.sessions.get("session")
    other_state = SessionInputRuntimeState.model_validate(
        state.model_copy(
            update={
                "session_id": "other",
                "revision": 1,
                "created_at": NOW,
                "updated_at": NOW,
            }
        ).model_dump(mode="python")
    )
    await repositories.sessions.create_if_absent(other_state)
    corrupt = original.model_copy(
        update={
            "admission_id": new_admission_id(),
            "session_id": "other",
            "input_batch_id": "other-input",
            "idempotency_key": "committed-input:other-input",
        }
    )
    layout = _Layout(tmp_path)
    atomic_write_model(
        layout.admission("other", corrupt.admission_id),
        corrupt,
    )

    _, gate, recovery = await recover(tmp_path, reader)
    with pytest.raises(InputRuntimeRecoveryError) as error:
        await recovery.recover()
    assert error.value.reason_code == "divergent_cycle_authority"
    assert gate.state == InputRuntimeLifecycleState.FAILED
    assert outcome.target_cycle_id == corrupt.target_cycle_id


@pytest.mark.asyncio
async def test_duplicate_admission_session_sequence_is_rejected(tmp_path):
    reader = Reader(Batch("initial", 1), Batch("corrupt", 2))
    repositories, _, _ = await seed_running(tmp_path, reader)
    original = await repositories.admissions.get_by_input_batch_id("initial")
    # Keep both records individually valid. The contradiction under test is the
    # duplicate immutable session/cycle sequence, not malformed START_CYCLE data.
    corrupt = original.model_copy(
        update={
            "admission_id": new_admission_id(),
            "input_batch_id": "corrupt",
            "idempotency_key": "committed-input:corrupt",
        }
    )
    atomic_write_model(
        _Layout(tmp_path).admission("session", corrupt.admission_id),
        corrupt,
    )

    _, gate, recovery = await recover(tmp_path, reader)
    with pytest.raises(InputRuntimeRecoveryError) as error:
        await recovery.recover()
    assert error.value.reason_code == "duplicate_admission_sequence"
    assert gate.state == InputRuntimeLifecycleState.FAILED


@pytest.mark.asyncio
async def test_missing_committed_batch_referenced_by_admission_is_fatal(tmp_path):
    original_reader = Reader(Batch("initial", 1))
    await seed_running(tmp_path, original_reader)
    empty_reader = Reader()
    _, gate, recovery = await recover(tmp_path, empty_reader)
    with pytest.raises(InputRuntimeRecoveryError) as error:
        await recovery.recover()
    assert error.value.reason_code == "missing_referenced_committed_batch"
    assert gate.state == InputRuntimeLifecycleState.FAILED


@pytest.mark.asyncio
async def test_missing_active_context_revision_is_fatal_not_partial_resume(tmp_path):
    reader = Reader(Batch("initial", 1))
    repositories, _, outcome = await seed_running(tmp_path, reader)
    snapshot = await repositories.snapshots.get(outcome.target_cycle_id)
    revision_path = _Layout(tmp_path).revision(
        outcome.target_cycle_id,
        snapshot.active_context_revision_id,
    )
    revision_path.unlink()

    _, gate, recovery = await recover(tmp_path, reader)
    with pytest.raises(InputRuntimeRecoveryError) as error:
        await recovery.recover()
    assert error.value.reason_code == "active_context_revision_missing"
    assert gate.state == InputRuntimeLifecycleState.FAILED


@pytest.mark.asyncio
async def test_multiple_nonterminal_handoffs_for_same_cycle_are_rejected(tmp_path):
    reader = Reader(Batch("initial", 1), Batch("addition", 2))
    repositories, runtime, outcome = await seed_running(tmp_path, reader)
    addition = await runtime.admit_committed_batch("addition", session_id="session")
    assert await runtime.begin_runtime_handoff(
        outcome.admission,
        handoff_token="initial-token",
    )
    clear_runtime_handoff_context_for_tests()
    assert await runtime.begin_runtime_handoff(
        addition.admission,
        handoff_token="addition-token",
    )
    clear_runtime_handoff_context_for_tests()

    _, gate, recovery = await recover(tmp_path, reader)
    with pytest.raises(InputRuntimeRecoveryError) as error:
        await recovery.recover()
    assert error.value.reason_code == "multiple_nonterminal_runtime_handoffs"
    assert gate.state == InputRuntimeLifecycleState.FAILED
