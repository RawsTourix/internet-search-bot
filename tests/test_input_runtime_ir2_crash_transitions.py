import asyncio
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.input_runtime import (
    AdmissionKind,
    CycleInboxItem,
    CycleStatus,
    InputAdmissionRecord,
    SessionInputRuntimeState,
    create_filesystem_input_runtime_repositories,
    new_context_revision_id,
    new_finalization_id,
)
from src.input_runtime import filesystem as filesystem_module
from src.input_runtime import _filesystem_session as session_repository_module
from src.storage import StorageConfigType

NOW = datetime(2026, 8, 5, tzinfo=timezone.utc)


def repositories(tmp_path):
    return create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )


def state(
    *,
    status: CycleStatus = CycleStatus.IDLE,
    active_cycle_id: str | None = None,
    accepted_session: int = 0,
    accepted_cycle: int = 0,
    applied_cycle: int = 0,
    pending_control: int = 0,
    applied_control: int = 0,
    context_revision_id: str | None = None,
    finalization_id: str | None = None,
):
    return SessionInputRuntimeState(
        session_id="session",
        generation=0,
        active_cycle_id=active_cycle_id,
        cycle_status=status,
        accepted_through_session_sequence=accepted_session,
        active_cycle_accepted_through_sequence=accepted_cycle,
        active_cycle_applied_through_sequence=applied_cycle,
        pending_control_sequence=pending_control,
        applied_control_sequence=applied_control,
        active_context_revision_id=context_revision_id,
        finalization_id=finalization_id,
        created_at=NOW,
        updated_at=NOW,
    )


def admission(
    batch_id: str,
    *,
    cycle_id: str,
    kind: AdmissionKind,
):
    return InputAdmissionRecord(
        session_id="session",
        input_batch_id=batch_id,
        session_sequence=1,
        target_cycle_id=cycle_id,
        cycle_sequence=0 if kind == AdmissionKind.START_CYCLE else 1,
        admitted_generation=0,
        admission_kind=kind,
        idempotency_key=f"key-{batch_id}",
        admitted_at=NOW,
    )


def test_duplicate_retry_repairs_state_after_admission_write_crash(
    tmp_path,
    monkeypatch,
):
    async def scenario():
        first = repositories(tmp_path)
        await first.sessions.create_if_absent(state())
        initial = admission(
            "batch-1",
            cycle_id="cycle-1",
            kind=AdmissionKind.START_CYCLE,
        )

        real_atomic_write = session_repository_module.atomic_write_model
        state_path = first.admissions.layout.state("session")
        failed = False

        def fail_first_state_write(path, model):
            nonlocal failed
            if path == state_path and not failed:
                failed = True
                raise OSError("simulated state write interruption")
            return real_atomic_write(path, model)

        monkeypatch.setattr(
            session_repository_module,
            "atomic_write_model",
            fail_first_state_write,
        )
        with pytest.raises(OSError, match="simulated state write interruption"):
            await first.admissions.allocate(initial)

        persisted = await first.admissions.get_by_input_batch_id("batch-1")
        assert persisted is not None
        stale_state = await first.sessions.get("session")
        assert stale_state.accepted_through_session_sequence == 0

        monkeypatch.setattr(
            session_repository_module,
            "atomic_write_model",
            real_atomic_write,
        )
        restarted = repositories(tmp_path)
        duplicate = await restarted.admissions.allocate(initial)
        assert duplicate == persisted

        repaired = await restarted.sessions.get("session")
        assert repaired.accepted_through_session_sequence == 1
        assert repaired.active_cycle_id == "cycle-1"
        assert repaired.cycle_status == CycleStatus.RUNNING

        next_record = await restarted.admissions.allocate(
            admission(
                "batch-2",
                cycle_id="cycle-1",
                kind=AdmissionKind.CONTINUE_RUNNING,
            )
        )
        assert next_record.session_sequence == 2
        assert next_record.cycle_sequence == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "terminal_status",
    [CycleStatus.DONE, CycleStatus.ERROR, CycleStatus.CANCELLED],
)
def test_terminal_session_starts_new_cycle_without_inheriting_projection(
    tmp_path,
    terminal_status,
):
    async def scenario():
        repository_bundle = repositories(tmp_path)
        old_context = new_context_revision_id()
        old_finalization = new_finalization_id()
        await repository_bundle.sessions.create_if_absent(
            state(
                status=terminal_status,
                active_cycle_id="old-cycle",
                accepted_session=5,
                accepted_cycle=3,
                applied_cycle=3,
                pending_control=4,
                applied_control=4,
                context_revision_id=old_context,
                finalization_id=old_finalization,
            )
        )

        allocated = await repository_bundle.admissions.allocate(
            admission(
                "new-batch",
                cycle_id="new-cycle",
                kind=AdmissionKind.START_CYCLE,
            )
        )
        assert allocated.session_sequence == 6
        assert allocated.cycle_sequence == 0

        updated = await repository_bundle.sessions.get("session")
        assert updated.active_cycle_id == "new-cycle"
        assert updated.cycle_status == CycleStatus.RUNNING
        assert updated.active_context_revision_id is None
        assert updated.finalization_id is None
        assert updated.active_cycle_accepted_through_sequence == 0
        assert updated.active_cycle_applied_through_sequence == 0
        assert updated.pending_control_sequence == 4
        assert updated.applied_control_sequence == 4
        SessionInputRuntimeState.model_validate(updated.model_dump(mode="json"))

    asyncio.run(scenario())


def test_finalization_id_is_rejected_outside_finalizing_or_terminal_state():
    with pytest.raises(ValidationError, match="finalization_id"):
        state(
            status=CycleStatus.RUNNING,
            active_cycle_id="cycle",
            finalization_id=new_finalization_id(),
        )

    with pytest.raises(ValidationError, match="finalization_id"):
        state(finalization_id=new_finalization_id())


def test_public_factory_uses_explicit_inbox_idempotency_relation(tmp_path):
    assert not hasattr(filesystem_module, "_same_inbox_idempotency")
    assert not hasattr(filesystem_module, "_filesystem_cycle")

    async def scenario():
        repository_bundle = repositories(tmp_path)
        original = CycleInboxItem(
            admission_id="adm_" + "1" * 32,
            session_id="session",
            cycle_id="cycle",
            input_batch_id="batch",
            cycle_sequence=1,
            generation=0,
            payload_size_bytes=10,
            enqueued_at=NOW,
        )
        assert await repository_bundle.inbox.create_if_absent(original) == original

        duplicate = original.model_copy(
            update={
                "inbox_item_id": "inbx_" + "2" * 32,
                "cycle_sequence": 99,
            }
        )
        assert await repository_bundle.inbox.create_if_absent(duplicate) == original

    asyncio.run(scenario())
