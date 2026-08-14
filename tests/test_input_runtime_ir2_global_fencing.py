import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.input_runtime import (
    ActiveCycleSnapshot,
    AdmissionKind,
    AgentEmission,
    CheckpointName,
    ControlCommandType,
    CycleContextRevision,
    CycleFinalizationRecord,
    CycleStatus,
    FinalizationState,
    InputAdmissionRecord,
    SessionControlCommand,
    SessionInputRuntimeState,
    create_filesystem_input_runtime_repositories,
    new_context_revision_id,
    new_control_id,
    new_emission_id,
    new_finalization_id,
)
from src.input_runtime.coordination import SessionLockRegistry
from src.input_runtime.errors import InputRuntimeConflictError
from src.input_runtime.serialization import atomic_write_model, read_model
from src.storage import StorageConfigType


NOW = datetime(2026, 8, 6, tzinfo=timezone.utc)


def repositories(path, *, locks=None):
    kwargs = {}
    if locks is not None:
        kwargs["locks"] = locks
    return create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(path)),
        **kwargs,
    )


def session(
    session_id: str,
    *,
    updated_at: datetime = NOW,
) -> SessionInputRuntimeState:
    return SessionInputRuntimeState(
        session_id=session_id,
        generation=0,
        created_at=NOW,
        updated_at=updated_at,
    )


def admission(
    batch: str,
    *,
    session_id: str,
    cycle_id: str,
    kind: AdmissionKind,
    session_sequence: int = 1,
    cycle_sequence: int | None = None,
    admitted_at: datetime = NOW,
) -> InputAdmissionRecord:
    return InputAdmissionRecord(
        session_id=session_id,
        input_batch_id=batch,
        session_sequence=session_sequence,
        target_cycle_id=cycle_id,
        cycle_sequence=(
            0 if kind == AdmissionKind.START_CYCLE else 1
        ) if cycle_sequence is None else cycle_sequence,
        admitted_generation=0,
        payload_size_bytes=1,
        admission_kind=kind,
        idempotency_key=f"key:{session_id}:{batch}",
        admitted_at=admitted_at,
    )


def control(
    *,
    control_id: str,
    session_id: str,
    cycle_id: str,
) -> SessionControlCommand:
    return SessionControlCommand(
        control_id=control_id,
        session_id=session_id,
        target_cycle_id=cycle_id,
        generation=0,
        sequence_number=1,
        command=ControlCommandType.PAUSE,
        idempotency_key=f"control:{session_id}",
        source_client_type="test",
        created_at=NOW,
    )


def snapshot(
    *,
    session_id: str,
    cycle_id: str,
) -> ActiveCycleSnapshot:
    return ActiveCycleSnapshot(
        cycle_id=cycle_id,
        session_id=session_id,
        generation=0,
        status=CycleStatus.RUNNING,
        original_input_batch_id=f"batch:{session_id}",
        original_user_request="request",
        active_context_revision_id=new_context_revision_id(),
        safe_checkpoint=CheckpointName.RESUME,
        created_at=NOW,
        updated_at=NOW,
    )


def context_revision(
    *,
    context_revision_id: str,
    session_id: str,
    cycle_id: str,
) -> CycleContextRevision:
    return CycleContextRevision(
        context_revision_id=context_revision_id,
        cycle_id=cycle_id,
        session_id=session_id,
        revision_number=1,
        reason="initial_input",
        created_at=NOW,
    )


def emission(
    *,
    emission_id: str,
    session_id: str,
    cycle_id: str,
) -> AgentEmission:
    return AgentEmission(
        emission_id=emission_id,
        session_id=session_id,
        cycle_id=cycle_id,
        generation=0,
        context_revision_id=new_context_revision_id(),
        kind="intermediate",
        text=f"message:{session_id}",
        response_route={"client": "test"},
        idempotency_key=f"emission:{session_id}",
        created_at=NOW,
    )


def finalization(
    *,
    finalization_id: str,
    session_id: str,
    cycle_id: str,
) -> CycleFinalizationRecord:
    return CycleFinalizationRecord(
        finalization_id=finalization_id,
        session_id=session_id,
        cycle_id=cycle_id,
        generation=0,
        context_revision_id=new_context_revision_id(),
        expected_accepted_sequence=0,
        expected_applied_sequence=0,
        expected_control_sequence=0,
        state=FinalizationState.PREPARED,
        created_at=NOW,
        updated_at=NOW,
    )


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
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    conflicts = [
        result
        for result in results
        if isinstance(result, InputRuntimeConflictError)
    ]
    assert len(conflicts) == 1


@pytest.mark.asyncio
async def test_allocation_repair_is_session_local(tmp_path, monkeypatch):
    repo = repositories(tmp_path)
    await repo.sessions.create_if_absent(session("s1"))
    await repo.sessions.create_if_absent(session("s2"))
    await repo.admissions.allocate(
        admission(
            "initial-1",
            session_id="s1",
            cycle_id="cycle-1",
            kind=AdmissionKind.START_CYCLE,
        )
    )
    await repo.admissions.allocate(
        admission(
            "initial-2",
            session_id="s2",
            cycle_id="cycle-2",
            kind=AdmissionKind.START_CYCLE,
        )
    )

    def forbidden_global_scan():
        raise AssertionError("allocation must not scan other sessions")

    monkeypatch.setattr(repo.admissions, "_scan", forbidden_global_scan)
    added = await repo.admissions.allocate(
        admission(
            "addition-1",
            session_id="s1",
            cycle_id="cycle-1",
            kind=AdmissionKind.CONTINUE_RUNNING,
        )
    )
    assert (added.session_sequence, added.cycle_sequence) == (2, 1)


@pytest.mark.asyncio
async def test_repair_preserves_monotonic_updated_at(tmp_path):
    repo = repositories(tmp_path)
    later = NOW + timedelta(minutes=10)
    await repo.sessions.create_if_absent(
        session("s", updated_at=later)
    )
    await repo.admissions.create_if_absent(
        admission(
            "initial",
            session_id="s",
            cycle_id="cycle",
            kind=AdmissionKind.START_CYCLE,
            admitted_at=NOW,
        )
    )

    added = await repo.admissions.allocate(
        admission(
            "addition",
            session_id="s",
            cycle_id="cycle",
            kind=AdmissionKind.CONTINUE_RUNNING,
            admitted_at=NOW,
        )
    )
    assert added.session_sequence == 2
    repaired = await repo.sessions.get("s")
    assert repaired is not None
    assert repaired.updated_at == later


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ["gap", "duplicate"])
async def test_repair_rejects_active_cycle_sequence_corruption(
    tmp_path,
    corruption,
):
    repo = repositories(tmp_path)
    await repo.sessions.create_if_absent(session("s"))
    records = [
        admission(
            "start",
            session_id="s",
            cycle_id="cycle",
            kind=AdmissionKind.START_CYCLE,
            session_sequence=1,
            cycle_sequence=0,
        )
    ]
    if corruption == "gap":
        records.append(
            admission(
                "bad-gap",
                session_id="s",
                cycle_id="cycle",
                kind=AdmissionKind.CONTINUE_RUNNING,
                session_sequence=2,
                cycle_sequence=2,
            )
        )
    else:
        records.extend(
            [
                admission(
                    "one",
                    session_id="s",
                    cycle_id="cycle",
                    kind=AdmissionKind.CONTINUE_RUNNING,
                    session_sequence=2,
                    cycle_sequence=1,
                ),
                admission(
                    "duplicate",
                    session_id="s",
                    cycle_id="cycle",
                    kind=AdmissionKind.CONTINUE_RUNNING,
                    session_sequence=3,
                    cycle_sequence=1,
                ),
            ]
        )

    for record in records:
        repo.admissions._ensure_cycle_authority(
            record.target_cycle_id,
            record.session_id,
        )
        repo.admissions._index_record(record)
        atomic_write_model(
            repo.admissions.layout.admission(
                record.session_id,
                record.admission_id,
            ),
            record,
        )

    with pytest.raises(InputRuntimeConflictError, match=corruption):
        await repo.admissions.allocate(
            admission(
                "next",
                session_id="s",
                cycle_id="cycle",
                kind=AdmissionKind.CONTINUE_RUNNING,
            )
        )


class PausingRegistry(SessionLockRegistry):
    def __init__(self) -> None:
        super().__init__(max_entries=1)
        self.after_acquire = asyncio.Event()
        self.release_hook = asyncio.Event()
        self.pause_once = True

    async def _after_lock_acquired(self, key, entry) -> None:
        if self.pause_once:
            self.pause_once = False
            self.after_acquire.set()
            await self.release_hook.wait()


@pytest.mark.asyncio
async def test_lock_cancellation_after_acquire_repairs_bookkeeping(tmp_path):
    registry = PausingRegistry()

    async def hold_cancelled():
        async with registry.hold(tmp_path, "cancelled"):
            raise AssertionError("cancelled task must not enter body")

    task = asyncio.create_task(hold_cancelled())
    await registry.after_acquire.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(registry._entries) == 1
    entry = next(iter(registry._entries.values()))
    assert entry.references == 0
    assert entry.waiters == 0
    assert entry.owners == 0
    assert not entry.lock.locked()

    async with registry.hold(tmp_path, "replacement"):
        pass
    await registry.cleanup()
    assert registry.size <= 1


@pytest.mark.asyncio
async def test_global_control_id_fencing_across_sessions(tmp_path):
    one = repositories(tmp_path)
    two = repositories(tmp_path)
    record_id = new_control_id()
    index = one.controls.layout.record_index("control", record_id)
    assert not index.exists()
    assert not two.controls.layout.record_index("control", record_id).exists()

    results = await run_together(
        lambda: one.controls.append(
            control(
                control_id=record_id,
                session_id="s1",
                cycle_id="c1",
            )
        ),
        lambda: two.controls.append(
            control(
                control_id=record_id,
                session_id="s2",
                cycle_id="c2",
            )
        ),
    )
    assert_one_success_one_conflict(results)
    records = [
        read_model(path, SessionControlCommand)
        for path in one.controls.layout.root.glob(
            "sessions/*/controls/*.json"
        )
        if read_model(path, SessionControlCommand).control_id == record_id
    ]
    assert len(records) == 1
    index.unlink()
    assert (await one.controls._find(record_id)).control_id == record_id


@pytest.mark.asyncio
async def test_global_context_revision_id_fencing_across_cycles(tmp_path):
    one = repositories(tmp_path)
    two = repositories(tmp_path)
    record_id = new_context_revision_id()
    index = one.context_revisions.layout.record_index(
        "revision",
        record_id,
    )
    assert not index.exists()

    results = await run_together(
        lambda: one.context_revisions.append_revision(
            context_revision(
                context_revision_id=record_id,
                session_id="s1",
                cycle_id="c1",
            )
        ),
        lambda: two.context_revisions.append_revision(
            context_revision(
                context_revision_id=record_id,
                session_id="s2",
                cycle_id="c2",
            )
        ),
    )
    assert_one_success_one_conflict(results)
    records = [
        read_model(path, CycleContextRevision)
        for path in one.context_revisions.layout.root.glob(
            "cycles/*/context-revisions/*.json"
        )
        if read_model(
            path,
            CycleContextRevision,
        ).context_revision_id == record_id
    ]
    assert len(records) == 1
    index.unlink()
    assert (
        await one.context_revisions.get(record_id)
    ).context_revision_id == record_id


@pytest.mark.asyncio
async def test_global_emission_id_fencing_across_cycles(tmp_path):
    one = repositories(tmp_path)
    two = repositories(tmp_path)
    record_id = new_emission_id()
    index = one.emissions.layout.record_index("emission", record_id)
    assert not index.exists()

    results = await run_together(
        lambda: one.emissions.create_if_absent(
            emission(
                emission_id=record_id,
                session_id="s1",
                cycle_id="c1",
            )
        ),
        lambda: two.emissions.create_if_absent(
            emission(
                emission_id=record_id,
                session_id="s2",
                cycle_id="c2",
            )
        ),
    )
    assert_one_success_one_conflict(results)
    assert len(
        [item for item in one.emissions._scan() if item.emission_id == record_id]
    ) == 1
    index.unlink()
    assert (await one.emissions._find(record_id)).emission_id == record_id


@pytest.mark.asyncio
async def test_global_finalization_id_fencing_across_cycles(tmp_path):
    one = repositories(tmp_path)
    two = repositories(tmp_path)
    record_id = new_finalization_id()
    index = one.finalizations.layout.record_index(
        "finalization",
        record_id,
    )
    assert not index.exists()

    results = await run_together(
        lambda: one.finalizations.prepare(
            finalization(
                finalization_id=record_id,
                session_id="s1",
                cycle_id="c1",
            )
        ),
        lambda: two.finalizations.prepare(
            finalization(
                finalization_id=record_id,
                session_id="s2",
                cycle_id="c2",
            )
        ),
    )
    assert_one_success_one_conflict(results)
    assert len(
        [
            item
            for item in one.finalizations._scan()
            if item.finalization_id == record_id
        ]
    ) == 1
    index.unlink()
    assert (
        await one.finalizations.get(record_id)
    ).finalization_id == record_id


@pytest.mark.asyncio
async def test_snapshot_and_context_share_global_cycle_authority(tmp_path):
    one = repositories(tmp_path)
    two = repositories(tmp_path)
    cycle_id = "shared-cycle"
    authority = one.snapshots.layout.cycle_authority(cycle_id)
    assert not authority.exists()
    revision_id = new_context_revision_id()

    results = await run_together(
        lambda: one.snapshots.create_if_absent(
            snapshot(session_id="s1", cycle_id=cycle_id)
        ),
        lambda: two.context_revisions.append_revision(
            context_revision(
                context_revision_id=revision_id,
                session_id="s2",
                cycle_id=cycle_id,
            )
        ),
    )
    assert_one_success_one_conflict(results)
    pointer = one.snapshots._read_pointer(authority)
    assert pointer is not None
    assert pointer.session_id in {"s1", "s2"}
    durable_count = int(
        one.snapshots.layout.snapshot(cycle_id).exists()
    ) + len(
        list(
            one.context_revisions.layout.revisions(cycle_id).glob(
                "*.json"
            )
        )
    )
    assert durable_count == 1
