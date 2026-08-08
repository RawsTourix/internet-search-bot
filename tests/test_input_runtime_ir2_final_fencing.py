import asyncio
from datetime import datetime, timezone

import pytest

import src.input_runtime._filesystem_identity as identity_module
from src.input_runtime import (
    AdmissionKind,
    CycleInboxItem,
    InputAdmissionRecord,
    SessionInputRuntimeState,
    create_filesystem_input_runtime_repositories,
    new_admission_id,
)
from src.input_runtime.errors import InputRuntimeConflictError
from src.storage import StorageConfigType


NOW = datetime(2026, 8, 5, tzinfo=timezone.utc)


def repositories(path):
    return create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(path))
    )


def session(session_id: str) -> SessionInputRuntimeState:
    return SessionInputRuntimeState(
        session_id=session_id,
        generation=0,
        created_at=NOW,
        updated_at=NOW,
    )


def admission(
    batch: str,
    *,
    session_id: str,
    cycle_id: str,
    kind: AdmissionKind,
    admission_id: str | None = None,
) -> InputAdmissionRecord:
    values = {
        "session_id": session_id,
        "input_batch_id": batch,
        "session_sequence": 1,
        "target_cycle_id": cycle_id,
        "cycle_sequence": 0 if kind == AdmissionKind.START_CYCLE else 1,
        "admitted_generation": 0,
        "payload_size_bytes": 1,
        "admission_kind": kind,
        "idempotency_key": f"key:{session_id}:{batch}",
        "admitted_at": NOW,
    }
    if admission_id is not None:
        values["admission_id"] = admission_id
    return InputAdmissionRecord(**values)


def fail_next_state_write(monkeypatch):
    real_write = identity_module.atomic_write_model
    failed = False

    def crashing_write(path, model):
        nonlocal failed
        if not failed and path.name == "state.json":
            failed = True
            raise RuntimeError("simulated state write crash")
        real_write(path, model)

    monkeypatch.setattr(identity_module, "atomic_write_model", crashing_write)
    return real_write


@pytest.mark.asyncio
async def test_new_batch_repairs_crashed_initial_admission_before_allocation(
    tmp_path,
    monkeypatch,
):
    first = repositories(tmp_path)
    await first.sessions.create_if_absent(session("s"))
    real_write = fail_next_state_write(monkeypatch)

    with pytest.raises(RuntimeError, match="simulated state write crash"):
        await first.admissions.allocate(
            admission(
                "A",
                session_id="s",
                cycle_id="cycle-a",
                kind=AdmissionKind.START_CYCLE,
            )
        )

    monkeypatch.setattr(identity_module, "atomic_write_model", real_write)
    restarted = repositories(tmp_path)
    second = await restarted.admissions.allocate(
        admission(
            "B",
            session_id="s",
            cycle_id="cycle-a",
            kind=AdmissionKind.CONTINUE_RUNNING,
        )
    )

    state = await restarted.sessions.get("s")
    assert state is not None
    assert state.active_cycle_id == "cycle-a"
    assert state.accepted_through_session_sequence == 2
    assert state.active_cycle_accepted_through_sequence == 1
    assert (second.session_sequence, second.cycle_sequence) == (2, 1)
    assert [
        row.session_sequence
        for row in await restarted.admissions.list_for_session("s")
    ] == [1, 2]


@pytest.mark.asyncio
async def test_new_batch_repairs_crashed_running_addition_before_allocation(
    tmp_path,
    monkeypatch,
):
    first = repositories(tmp_path)
    await first.sessions.create_if_absent(session("s"))
    await first.admissions.allocate(
        admission(
            "initial",
            session_id="s",
            cycle_id="cycle-a",
            kind=AdmissionKind.START_CYCLE,
        )
    )

    real_write = fail_next_state_write(monkeypatch)
    with pytest.raises(RuntimeError, match="simulated state write crash"):
        await first.admissions.allocate(
            admission(
                "A",
                session_id="s",
                cycle_id="cycle-a",
                kind=AdmissionKind.CONTINUE_RUNNING,
            )
        )

    monkeypatch.setattr(identity_module, "atomic_write_model", real_write)
    restarted = repositories(tmp_path)
    second = await restarted.admissions.allocate(
        admission(
            "B",
            session_id="s",
            cycle_id="cycle-a",
            kind=AdmissionKind.CONTINUE_RUNNING,
        )
    )

    state = await restarted.sessions.get("s")
    assert state is not None
    assert state.accepted_through_session_sequence == 3
    assert state.active_cycle_accepted_through_sequence == 2
    assert (second.session_sequence, second.cycle_sequence) == (3, 2)
    assert [
        row.session_sequence
        for row in await restarted.admissions.list_for_session("s")
    ] == [1, 2, 3]


async def run_together(*calls):
    ready = 0
    guard = asyncio.Lock()
    release = asyncio.Event()

    async def wrapped(call):
        nonlocal ready
        async with guard:
            ready += 1
            if ready == len(calls):
                release.set()
        await release.wait()
        return await call()

    return await asyncio.gather(
        *(wrapped(call) for call in calls),
        return_exceptions=True,
    )


def assert_one_success_one_conflict(results):
    assert sum(not isinstance(result, Exception) for result in results) == 1
    conflicts = [
        result
        for result in results
        if isinstance(result, InputRuntimeConflictError)
    ]
    assert len(conflicts) == 1


@pytest.mark.asyncio
async def test_cross_session_global_admission_identity_fencing(tmp_path):
    one = repositories(tmp_path)
    two = repositories(tmp_path)
    await one.sessions.create_if_absent(session("s1"))
    await two.sessions.create_if_absent(session("s2"))

    same_batch = await run_together(
        lambda: one.admissions.allocate(
            admission(
                "shared",
                session_id="s1",
                cycle_id="c1",
                kind=AdmissionKind.START_CYCLE,
            )
        ),
        lambda: two.admissions.allocate(
            admission(
                "shared",
                session_id="s2",
                cycle_id="c2",
                kind=AdmissionKind.START_CYCLE,
            )
        ),
    )
    assert_one_success_one_conflict(same_batch)
    assert len([row for row in one.admissions._scan() if row.input_batch_id == "shared"]) == 1

    fixed_id = new_admission_id()
    same_id = await run_together(
        lambda: one.admissions.allocate(
            admission(
                "id-a",
                session_id="s1",
                cycle_id="id-cycle-a",
                kind=AdmissionKind.START_CYCLE,
                admission_id=fixed_id,
            )
        ),
        lambda: two.admissions.allocate(
            admission(
                "id-b",
                session_id="s2",
                cycle_id="id-cycle-b",
                kind=AdmissionKind.START_CYCLE,
                admission_id=fixed_id,
            )
        ),
    )
    assert_one_success_one_conflict(same_id)
    assert len([row for row in one.admissions._scan() if row.admission_id == fixed_id]) == 1


@pytest.mark.asyncio
async def test_cross_session_cycle_and_inbox_identity_fencing(tmp_path):
    one = repositories(tmp_path)
    two = repositories(tmp_path)
    await one.sessions.create_if_absent(session("x1"))
    await two.sessions.create_if_absent(session("x2"))

    same_cycle = await run_together(
        lambda: one.admissions.allocate(
            admission(
                "cycle-a",
                session_id="x1",
                cycle_id="shared-cycle",
                kind=AdmissionKind.START_CYCLE,
            )
        ),
        lambda: two.admissions.allocate(
            admission(
                "cycle-b",
                session_id="x2",
                cycle_id="shared-cycle",
                kind=AdmissionKind.START_CYCLE,
            )
        ),
    )
    assert_one_success_one_conflict(same_cycle)

    first_item = CycleInboxItem(
        admission_id="adm_" + "1" * 32,
        session_id="x1",
        cycle_id="inbox-cycle-a",
        input_batch_id="shared-inbox-input",
        cycle_sequence=1,
        generation=0,
        payload_size_bytes=1,
        enqueued_at=NOW,
    )
    second_item = CycleInboxItem(
        admission_id=first_item.admission_id,
        session_id="x2",
        cycle_id="inbox-cycle-b",
        input_batch_id=first_item.input_batch_id,
        cycle_sequence=1,
        generation=0,
        payload_size_bytes=1,
        enqueued_at=NOW,
    )
    same_inbox = await run_together(
        lambda: one.inbox.create_if_absent(first_item),
        lambda: two.inbox.create_if_absent(second_item),
    )
    assert_one_success_one_conflict(same_inbox)
    matches = [
        row
        for row in one.inbox._scan()
        if row.admission_id == first_item.admission_id
        or row.input_batch_id == first_item.input_batch_id
    ]
    assert len(matches) == 1
