import asyncio
from datetime import datetime, timezone

import pytest

import src.input_runtime._filesystem_common as common_module
from src.input_runtime import (
    ActiveCycleSnapshot,
    AdmissionKind,
    AgentEmission,
    CheckpointName,
    ControlCommandType,
    CycleContextRevision,
    CycleFinalizationRecord,
    CycleInboxItem,
    CycleStatus,
    FinalizationState,
    InputAdmissionRecord,
    SessionControlCommand,
    create_filesystem_input_runtime_repositories,
    new_admission_id,
    new_context_revision_id,
    new_control_id,
    new_emission_id,
    new_finalization_id,
    new_inbox_item_id,
)
from src.input_runtime.errors import InputRuntimeConflictError
from src.input_runtime.serialization import read_model
from src.storage import StorageConfigType


NOW = datetime(2026, 8, 6, tzinfo=timezone.utc)
KINDS = (
    "admission",
    "inbox",
    "control",
    "snapshot",
    "context",
    "emission",
    "finalization",
)


def repositories(path):
    return create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(path))
    )


def build_record(
    kind: str,
    *,
    session_id: str,
    cycle_id: str,
    stable_id: str | None = None,
):
    if kind == "admission":
        return InputAdmissionRecord(
            admission_id=stable_id or new_admission_id(),
            session_id=session_id,
            input_batch_id=f"batch:{session_id}:{cycle_id}",
            session_sequence=1,
            target_cycle_id=cycle_id,
            cycle_sequence=0,
            admitted_generation=0,
            payload_size_bytes=1,
            admission_kind=AdmissionKind.START_CYCLE,
            idempotency_key=f"admission:{session_id}:{cycle_id}",
            admitted_at=NOW,
        )
    if kind == "inbox":
        return CycleInboxItem(
            inbox_item_id=stable_id or new_inbox_item_id(),
            admission_id=new_admission_id(),
            session_id=session_id,
            cycle_id=cycle_id,
            input_batch_id=f"batch:{session_id}:{cycle_id}",
            cycle_sequence=1,
            generation=0,
            payload_size_bytes=1,
            enqueued_at=NOW,
        )
    if kind == "control":
        return SessionControlCommand(
            control_id=stable_id or new_control_id(),
            session_id=session_id,
            target_cycle_id=cycle_id,
            generation=0,
            sequence_number=1,
            command=ControlCommandType.PAUSE,
            idempotency_key=f"control:{session_id}:{cycle_id}",
            source_client_type="test",
            created_at=NOW,
        )
    if kind == "snapshot":
        return ActiveCycleSnapshot(
            cycle_id=cycle_id,
            session_id=session_id,
            generation=0,
            status=CycleStatus.RUNNING,
            original_input_batch_id=f"batch:{session_id}:{cycle_id}",
            original_user_request="request",
            active_context_revision_id=new_context_revision_id(),
            safe_checkpoint=CheckpointName.RESUME,
            created_at=NOW,
            updated_at=NOW,
        )
    if kind == "context":
        return CycleContextRevision(
            context_revision_id=stable_id or new_context_revision_id(),
            cycle_id=cycle_id,
            session_id=session_id,
            revision_number=1,
            reason="initial_input",
            created_at=NOW,
        )
    if kind == "emission":
        return AgentEmission(
            emission_id=stable_id or new_emission_id(),
            session_id=session_id,
            cycle_id=cycle_id,
            generation=0,
            context_revision_id=new_context_revision_id(),
            kind="intermediate",
            text=f"message:{session_id}:{cycle_id}",
            response_route={"client": "test"},
            idempotency_key=f"emission:{session_id}:{cycle_id}",
            created_at=NOW,
        )
    if kind == "finalization":
        return CycleFinalizationRecord(
            finalization_id=stable_id or new_finalization_id(),
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
    raise AssertionError(kind)


async def create_record(bundle, kind: str, record):
    if kind == "admission":
        return await bundle.admissions.create_if_absent(record)
    if kind == "inbox":
        return await bundle.inbox.create_if_absent(record)
    if kind == "control":
        return await bundle.controls.append(record)
    if kind == "snapshot":
        return await bundle.snapshots.create_if_absent(record)
    if kind == "context":
        return await bundle.context_revisions.append_revision(record)
    if kind == "emission":
        return await bundle.emissions.create_if_absent(record)
    if kind == "finalization":
        return await bundle.finalizations.prepare(record)
    raise AssertionError(kind)


def repository_for(bundle, kind: str):
    return {
        "admission": bundle.admissions,
        "inbox": bundle.inbox,
        "control": bundle.controls,
        "snapshot": bundle.snapshots,
        "context": bundle.context_revisions,
        "emission": bundle.emissions,
        "finalization": bundle.finalizations,
    }[kind]


def stable_id(kind: str, record) -> str:
    return {
        "admission": record.admission_id,
        "inbox": record.inbox_item_id,
        "control": record.control_id,
        "snapshot": record.cycle_id,
        "context": record.context_revision_id,
        "emission": record.emission_id,
        "finalization": record.finalization_id,
    }[kind]


def record_cycle_id(kind: str, record) -> str:
    if kind in {"admission", "control"}:
        return record.target_cycle_id
    return record.cycle_id


def record_path(bundle, kind: str, record):
    repo = repository_for(bundle, kind)
    if kind == "admission":
        return repo.layout.admission(record.session_id, record.admission_id)
    if kind == "inbox":
        return repo.layout.inbox_item(record.cycle_id, record.inbox_item_id)
    if kind == "control":
        return repo.layout.control(record.session_id, record.control_id)
    if kind == "snapshot":
        return repo.layout.snapshot(record.cycle_id)
    if kind == "context":
        return repo.layout.revision(record.cycle_id, record.context_revision_id)
    if kind == "emission":
        return repo.layout.emission(record.cycle_id, record.emission_id)
    if kind == "finalization":
        return repo.layout.finalization(record.cycle_id, record.finalization_id)
    raise AssertionError(kind)


def index_paths(bundle, kind: str, record):
    repo = repository_for(bundle, kind)
    authority = repo.layout.cycle_authority(record_cycle_id(kind, record))
    if kind == "admission":
        return (
            repo.layout.record_index("admission", record.admission_id),
            repo.layout.admission_input(record.input_batch_id),
            authority,
        )
    if kind == "inbox":
        return (
            repo.layout.record_index("inbox", record.inbox_item_id),
            repo.layout.inbox_admission(record.admission_id),
            repo.layout.inbox_input(record.input_batch_id),
            authority,
        )
    if kind == "control":
        return (
            repo.layout.record_index("control", record.control_id),
            authority,
        )
    if kind == "snapshot":
        return (
            repo.layout.record_index("snapshot", record.cycle_id),
            authority,
        )
    if kind == "context":
        return (
            repo.layout.record_index("revision", record.context_revision_id),
            authority,
        )
    if kind == "emission":
        return (
            repo.layout.record_index("emission", record.emission_id),
            authority,
        )
    if kind == "finalization":
        return (
            repo.layout.record_index("finalization", record.finalization_id),
            authority,
        )
    raise AssertionError(kind)


def model_and_pattern(bundle, kind: str):
    root = repository_for(bundle, kind).layout.root
    return {
        "admission": (
            InputAdmissionRecord,
            root.glob("sessions/*/admissions/*.json"),
            "admission_id",
        ),
        "inbox": (
            CycleInboxItem,
            root.glob("cycles/*/inbox/*.json"),
            "inbox_item_id",
        ),
        "control": (
            SessionControlCommand,
            root.glob("sessions/*/controls/*.json"),
            "control_id",
        ),
        "snapshot": (
            ActiveCycleSnapshot,
            root.glob("cycles/*/snapshot.json"),
            "cycle_id",
        ),
        "context": (
            CycleContextRevision,
            root.glob("cycles/*/context-revisions/*.json"),
            "context_revision_id",
        ),
        "emission": (
            AgentEmission,
            root.glob("cycles/*/emissions/*.json"),
            "emission_id",
        ),
        "finalization": (
            CycleFinalizationRecord,
            root.glob("cycles/*/finalizations/*.json"),
            "finalization_id",
        ),
    }[kind]


def count_durable(bundle, kind: str, record) -> int:
    model_type, paths, field = model_and_pattern(bundle, kind)
    expected = stable_id(kind, record)
    return sum(
        getattr(read_model(path, model_type), field) == expected
        for path in paths
    )


def competing_record(kind: str, record):
    cycle = f"competing:{record_cycle_id(kind, record)}"
    return build_record(
        kind,
        session_id="session-2",
        cycle_id=(record.cycle_id if kind == "snapshot" else cycle),
        stable_id=stable_id(kind, record),
    )


def assert_indexes_rebuilt(bundle, kind: str, record) -> None:
    repo = repository_for(bundle, kind)
    authority_path = repo.layout.cycle_authority(
        record_cycle_id(kind, record)
    )
    for path in index_paths(bundle, kind, record):
        assert path.exists(), path
        pointer = repo._read_pointer(path)
        assert pointer is not None
        if path == authority_path:
            assert pointer.session_id == record.session_id
        else:
            assert repo._pointer_record_path(pointer).exists()


def fail_pointer_write(monkeypatch, *, number: int):
    real_write = common_module.atomic_write_model
    calls = 0

    def crashing_write(path, model):
        nonlocal calls
        calls += 1
        if calls == number:
            raise RuntimeError(f"crash at pointer write {number}")
        return real_write(path, model)

    monkeypatch.setattr(common_module, "atomic_write_model", crashing_write)
    return real_write


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_record_first_crash_recovers_before_competing_create(
    tmp_path,
    monkeypatch,
    kind,
):
    first = repositories(tmp_path)
    record = build_record(
        kind,
        session_id="session-1",
        cycle_id="cycle-1",
    )
    real_write = fail_pointer_write(monkeypatch, number=1)

    with pytest.raises(RuntimeError, match="pointer write 1"):
        await create_record(first, kind, record)

    assert record_path(first, kind, record).exists()
    assert count_durable(first, kind, record) == 1
    monkeypatch.setattr(common_module, "atomic_write_model", real_write)

    restarted_same = repositories(tmp_path)
    restarted_competing = repositories(tmp_path)
    same_result, competing_result = await asyncio.gather(
        create_record(restarted_same, kind, record),
        create_record(
            restarted_competing,
            kind,
            competing_record(kind, record),
        ),
        return_exceptions=True,
    )

    assert same_result == record
    assert isinstance(competing_result, InputRuntimeConflictError)
    assert count_durable(restarted_same, kind, record) == 1
    assert_indexes_rebuilt(restarted_same, kind, record)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_partial_index_crash_rebuilds_remaining_indexes(
    tmp_path,
    monkeypatch,
    kind,
):
    first = repositories(tmp_path)
    record = build_record(
        kind,
        session_id="session-1",
        cycle_id="cycle-1",
    )
    real_write = fail_pointer_write(monkeypatch, number=2)

    with pytest.raises(RuntimeError, match="pointer write 2"):
        await create_record(first, kind, record)

    assert record_path(first, kind, record).exists()
    paths = index_paths(first, kind, record)
    assert paths[0].exists()
    assert any(not path.exists() for path in paths[1:])
    monkeypatch.setattr(common_module, "atomic_write_model", real_write)

    restarted = repositories(tmp_path)
    assert await create_record(restarted, kind, record) == record
    assert count_durable(restarted, kind, record) == 1
    assert_indexes_rebuilt(restarted, kind, record)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_dangling_stable_index_is_cleared_and_reused(tmp_path, kind):
    bundle = repositories(tmp_path)
    record = build_record(
        kind,
        session_id="session-1",
        cycle_id="cycle-1",
    )
    repo = repository_for(bundle, kind)
    stable_index = index_paths(bundle, kind, record)[0]
    repo._write_pointer(
        stable_index,
        repo._pointer(
            kind,
            stable_id(kind, record),
            "ghost-session",
            record_path(bundle, kind, record),
            record_cycle_id(kind, record),
        ),
    )
    assert stable_index.exists()
    assert not record_path(bundle, kind, record).exists()

    assert await create_record(bundle, kind, record) == record
    assert count_durable(bundle, kind, record) == 1
    assert_indexes_rebuilt(bundle, kind, record)


@pytest.mark.asyncio
async def test_lost_admission_input_index_blocks_competing_session(tmp_path):
    first = repositories(tmp_path)
    record = build_record(
        "admission",
        session_id="session-1",
        cycle_id="cycle-1",
    )
    await create_record(first, "admission", record)
    input_index = first.admissions.layout.admission_input(record.input_batch_id)
    input_index.unlink()

    competing = build_record(
        "admission",
        session_id="session-2",
        cycle_id="cycle-2",
    ).model_copy(update={"input_batch_id": record.input_batch_id})
    restarted = repositories(tmp_path)
    with pytest.raises(InputRuntimeConflictError, match="relation changed"):
        await create_record(restarted, "admission", competing)

    assert input_index.exists()
    assert count_durable(restarted, "admission", record) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ("admission", "input"))
async def test_lost_inbox_relation_index_blocks_competing_session(
    tmp_path,
    identity,
):
    first = repositories(tmp_path)
    record = build_record(
        "inbox",
        session_id="session-1",
        cycle_id="cycle-1",
    )
    await create_record(first, "inbox", record)
    relation_index = (
        first.inbox.layout.inbox_admission(record.admission_id)
        if identity == "admission"
        else first.inbox.layout.inbox_input(record.input_batch_id)
    )
    relation_index.unlink()

    competing = build_record(
        "inbox",
        session_id="session-2",
        cycle_id="cycle-2",
    )
    updates = (
        {"admission_id": record.admission_id}
        if identity == "admission"
        else {"input_batch_id": record.input_batch_id}
    )
    competing = competing.model_copy(update=updates)
    restarted = repositories(tmp_path)
    with pytest.raises(InputRuntimeConflictError, match="identity conflict"):
        await create_record(restarted, "inbox", competing)

    assert relation_index.exists()
    assert count_durable(restarted, "inbox", record) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "identity",
    ("admission_input", "inbox_admission", "inbox_input"),
)
async def test_dangling_relation_pointer_without_record_is_replaced(
    tmp_path,
    identity,
):
    bundle = repositories(tmp_path)
    if identity == "admission_input":
        kind = "admission"
        record = build_record(
            kind,
            session_id="session-1",
            cycle_id="cycle-1",
        )
        repo = bundle.admissions
        index = repo.layout.admission_input(record.input_batch_id)
    else:
        kind = "inbox"
        record = build_record(
            kind,
            session_id="session-1",
            cycle_id="cycle-1",
        )
        repo = bundle.inbox
        index = (
            repo.layout.inbox_admission(record.admission_id)
            if identity == "inbox_admission"
            else repo.layout.inbox_input(record.input_batch_id)
        )
    repo._write_pointer(
        index,
        repo._pointer(
            "dangling",
            "missing",
            "ghost-session",
            record_path(bundle, kind, record),
            record_cycle_id(kind, record),
        ),
    )

    assert await create_record(bundle, kind, record) == record
    assert count_durable(bundle, kind, record) == 1
    assert_indexes_rebuilt(bundle, kind, record)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("existing_kind", "competing_kind"),
    (
        ("snapshot", "context"),
        ("context", "emission"),
        ("emission", "snapshot"),
    ),
)
async def test_lost_cycle_authority_is_rebuilt_before_competing_create(
    tmp_path,
    existing_kind,
    competing_kind,
):
    first = repositories(tmp_path)
    existing = build_record(
        existing_kind,
        session_id="session-1",
        cycle_id="shared-cycle",
    )
    await create_record(first, existing_kind, existing)
    authority = repository_for(first, existing_kind).layout.cycle_authority(
        "shared-cycle"
    )
    authority.unlink()

    restarted = repositories(tmp_path)
    competing = build_record(
        competing_kind,
        session_id="session-2",
        cycle_id="shared-cycle",
    )
    with pytest.raises(InputRuntimeConflictError, match="another session"):
        await create_record(restarted, competing_kind, competing)

    pointer = repository_for(restarted, existing_kind)._read_pointer(authority)
    assert pointer is not None
    assert pointer.session_id == "session-1"
    assert count_durable(restarted, existing_kind, existing) == 1
    assert not record_path(restarted, competing_kind, competing).exists()


@pytest.mark.asyncio
async def test_dangling_cycle_authority_without_record_is_replaced(tmp_path):
    bundle = repositories(tmp_path)
    record = build_record(
        "snapshot",
        session_id="session-1",
        cycle_id="cycle-1",
    )
    repo = bundle.snapshots
    authority = repo.layout.cycle_authority(record.cycle_id)
    repo._write_pointer(
        authority,
        repo._pointer(
            "cycle",
            record.cycle_id,
            "ghost-session",
            repo.layout.cycle_dir(record.cycle_id),
            record.cycle_id,
        ),
    )

    assert await create_record(bundle, "snapshot", record) == record
    pointer = repo._read_pointer(authority)
    assert pointer is not None
    assert pointer.session_id == record.session_id
    assert count_durable(bundle, "snapshot", record) == 1
