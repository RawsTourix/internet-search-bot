import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.input_runtime import (
    AdmissionKind,
    ActiveCycleSnapshot,
    AgentEmission,
    ControlCommandType,
    CycleFinalizationRecord,
    EmissionState,
    FinalizationState,
    SessionControlCommand,
    CheckpointName,
    CycleContextRevision,
    CycleStatus,
    InputAdmissionRecord,
    SessionInputRuntimeState,
    create_filesystem_input_runtime_repositories,
    new_context_revision_id,
)
from src.input_runtime.errors import InputRuntimeConflictError, InputRuntimeError
from src.input_runtime.serialization import atomic_write_model, storage_key
from src.storage import StorageConfigType

NOW = datetime(2026, 8, 5, tzinfo=timezone.utc)


def run(coro):
    return asyncio.run(coro)


def repositories(tmp_path: Path):
    return create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )


def state(session_id="session", revision=1):
    return SessionInputRuntimeState(
        session_id=session_id,
        generation=0,
        revision=revision,
        created_at=NOW,
        updated_at=NOW,
    )


def admission(*, batch="batch", session_sequence=1, cycle_sequence=0):
    return InputAdmissionRecord(
        session_id="session",
        input_batch_id=batch,
        session_sequence=session_sequence,
        target_cycle_id="cycle",
        cycle_sequence=cycle_sequence,
        admitted_generation=0,
        admission_kind=(
            AdmissionKind.START_CYCLE
            if cycle_sequence == 0
            else AdmissionKind.CONTINUE_RUNNING
        ),
        idempotency_key=f"key-{batch}",
        admitted_at=NOW,
    )


def snapshot(*, revision=1, applied=0, batches=None):
    return ActiveCycleSnapshot(
        cycle_id="cycle",
        session_id="session",
        generation=0,
        status=CycleStatus.RUNNING,
        original_input_batch_id="batch",
        original_user_request="request",
        applied_input_batch_ids=list(batches or []),
        applied_through_cycle_sequence=applied,
        active_context_revision_id=new_context_revision_id(),
        snapshot_revision=revision,
        safe_checkpoint=CheckpointName.BEFORE_LLM,
        created_at=NOW,
        updated_at=NOW,
    )


def test_session_state_cas_and_restart(tmp_path):
    first = repositories(tmp_path)
    created = run(first.sessions.create_if_absent(state()))
    assert created.revision == 1
    updated = state(revision=2)
    assert run(first.sessions.compare_and_swap(1, updated)).revision == 2
    with pytest.raises(InputRuntimeConflictError):
        run(first.sessions.compare_and_swap(1, state(revision=2)))
    recreated = repositories(tmp_path)
    assert run(recreated.sessions.get("session")) == updated
    assert run(recreated.sessions.list_states()) == (updated,)


def test_duplicate_admission_and_deterministic_sequences(tmp_path):
    repos = repositories(tmp_path)
    first = run(repos.admissions.create_if_absent(admission()))
    duplicate = run(repos.admissions.create_if_absent(
        admission(batch="batch", session_sequence=2, cycle_sequence=1)
    ))
    assert duplicate == first
    second = run(repos.admissions.allocate(
        admission(batch="batch-2", session_sequence=2, cycle_sequence=1)
    ))
    assert [item.session_sequence for item in run(
        repos.admissions.list_for_session("session")
    )] == [1, 2]
    assert second.cycle_sequence == 1
    with pytest.raises(InputRuntimeConflictError):
        run(repos.admissions.allocate(
            admission(batch="batch-3", session_sequence=2, cycle_sequence=2)
        ))


def test_context_revision_append_is_linear_and_restart_safe(tmp_path):
    repos = repositories(tmp_path)
    initial = CycleContextRevision(
        cycle_id="cycle",
        session_id="session",
        revision_number=1,
        reason="initial_input",
        created_at=NOW,
    )
    assert run(repos.context_revisions.append_revision(initial)) == initial
    second = CycleContextRevision(
        cycle_id="cycle",
        session_id="session",
        revision_number=2,
        parent_revision_ids=[initial.context_revision_id],
        reason="input_applied",
        created_at=NOW,
    )
    run(repos.context_revisions.append_revision(second))
    assert run(repositories(tmp_path).context_revisions.get_latest("cycle")) == second
    bad = CycleContextRevision(
        cycle_id="cycle",
        session_id="session",
        revision_number=4,
        parent_revision_ids=[second.context_revision_id],
        reason="input_applied",
        created_at=NOW,
    )
    with pytest.raises(InputRuntimeConflictError):
        run(repos.context_revisions.append_revision(bad))


def test_snapshot_cas_and_two_repository_instances(tmp_path):
    one = repositories(tmp_path)
    two = repositories(tmp_path)
    original = snapshot()
    run(one.snapshots.create_if_absent(original))
    updated = snapshot(revision=2)
    assert run(two.snapshots.compare_and_swap(1, updated)) == updated
    with pytest.raises(InputRuntimeConflictError):
        run(one.snapshots.compare_and_swap(1, updated))


def test_user_controlled_ids_are_hashed_not_path_segments(tmp_path):
    malicious = "../../outside/session"
    repos = repositories(tmp_path)
    record = state(session_id=malicious)
    run(repos.sessions.create_if_absent(record))
    expected = tmp_path / "input-runtime" / "sessions" / storage_key(malicious) / "state.json"
    assert expected.exists()
    assert not (tmp_path.parent / "outside").exists()


def test_atomic_write_failure_preserves_previous_record(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    original = state()
    atomic_write_model(path, original)

    def fail_replace(source, destination):
        raise OSError("simulated interruption")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(InputRuntimeError):
        atomic_write_model(path, state(revision=2))
    from src.input_runtime.serialization import read_model
    assert read_model(path, SessionInputRuntimeState) == original


def test_control_repository_is_idempotent_and_restart_safe(tmp_path):
    repos = repositories(tmp_path)
    command = SessionControlCommand(
        session_id="session",
        target_cycle_id="cycle",
        generation=0,
        sequence_number=1,
        command=ControlCommandType.PAUSE,
        idempotency_key="control-key",
        source_client_type="telegram",
        created_at=NOW,
    )
    assert run(repos.controls.append(command)) == command
    duplicate = command.model_copy(update={"control_id": "ctl_" + "1" * 32})
    assert run(repos.controls.append(duplicate)) == command
    acknowledged = run(repos.controls.acknowledge(
        command.control_id, acknowledged_at=NOW
    ))
    applied = run(repositories(tmp_path).controls.apply(
        command.control_id, applied_at=NOW
    ))
    assert acknowledged.acknowledged_at == NOW
    assert applied.applied_at == NOW
    assert run(repos.controls.list_pending("session", generation=0)) == ()


def test_emission_delivery_claim_is_fenced_and_durable(tmp_path):
    repos = repositories(tmp_path)
    emission = AgentEmission(
        session_id="session",
        cycle_id="cycle",
        context_revision_id=new_context_revision_id(),
        kind="intermediate",
        text="message",
        response_route={"client": "test"},
        idempotency_key="emission-key",
        created_at=NOW,
    )
    assert run(repos.emissions.create_if_absent(emission)) == emission
    claimed = run(repos.emissions.claim_delivery(
        emission.emission_id, claim_token="claim"
    ))
    assert claimed.state == EmissionState.DELIVERING
    with pytest.raises(InputRuntimeConflictError):
        run(repositories(tmp_path).emissions.complete_delivery(
            emission.emission_id, claim_token="stale", delivered_at=NOW
        ))
    delivered = run(repositories(tmp_path).emissions.complete_delivery(
        emission.emission_id, claim_token="claim", delivered_at=NOW
    ))
    assert delivered.state == EmissionState.DELIVERED


def test_finalization_transition_is_state_fenced(tmp_path):
    repos = repositories(tmp_path)
    prepared = CycleFinalizationRecord(
        session_id="session",
        cycle_id="cycle",
        generation=0,
        context_revision_id=new_context_revision_id(),
        expected_accepted_sequence=1,
        expected_applied_sequence=1,
        expected_control_sequence=0,
        state=FinalizationState.PREPARED,
        created_at=NOW,
        updated_at=NOW,
    )
    run(repos.finalizations.prepare(prepared))
    result = CycleFinalizationRecord.model_validate({
        **prepared.model_dump(mode="json"),
        "state": FinalizationState.RESULT_PERSISTED,
        "result_ref": "result",
    })
    assert run(repos.finalizations.advance(
        prepared.finalization_id,
        expected_state=FinalizationState.PREPARED,
        next_record=result,
    )) == result
    with pytest.raises(InputRuntimeConflictError):
        run(repos.finalizations.advance(
            prepared.finalization_id,
            expected_state=FinalizationState.PREPARED,
            next_record=result,
        ))
    assert run(repositories(tmp_path).finalizations.list_recoverable()) == (result,)
