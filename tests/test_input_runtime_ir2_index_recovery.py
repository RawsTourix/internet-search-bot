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
from src.input_runtime.serialization import atomic_write_model, read_model
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


def make(kind, session, cycle, stable=None):
    if kind == "admission":
        return InputAdmissionRecord(
            admission_id=stable or new_admission_id(),
            session_id=session,
            input_batch_id=f"batch:{session}:{cycle}",
            session_sequence=1,
            target_cycle_id=cycle,
            cycle_sequence=0,
            admitted_generation=0,
            payload_size_bytes=1,
            admission_kind=AdmissionKind.START_CYCLE,
            idempotency_key=f"admission:{session}:{cycle}",
            admitted_at=NOW,
        )
    if kind == "inbox":
        return CycleInboxItem(
            inbox_item_id=stable or new_inbox_item_id(),
            admission_id=new_admission_id(),
            session_id=session,
            cycle_id=cycle,
            input_batch_id=f"batch:{session}:{cycle}",
            cycle_sequence=1,
            generation=0,
            payload_size_bytes=1,
            enqueued_at=NOW,
        )
    if kind == "control":
        return SessionControlCommand(
            control_id=stable or new_control_id(),
            session_id=session,
            target_cycle_id=cycle,
            generation=0,
            sequence_number=1,
            command=ControlCommandType.PAUSE,
            idempotency_key=f"control:{session}:{cycle}",
            source_client_type="test",
            created_at=NOW,
        )
    if kind == "snapshot":
        return ActiveCycleSnapshot(
            cycle_id=cycle,
            session_id=session,
            generation=0,
            status=CycleStatus.RUNNING,
            original_input_batch_id=f"batch:{session}:{cycle}",
            original_user_request="request",
            active_context_revision_id=new_context_revision_id(),
            safe_checkpoint=CheckpointName.RESUME,
            created_at=NOW,
            updated_at=NOW,
        )
    if kind == "context":
        return CycleContextRevision(
            context_revision_id=stable or new_context_revision_id(),
            cycle_id=cycle,
            session_id=session,
            revision_number=1,
            reason="initial_input",
            created_at=NOW,
        )
    if kind == "emission":
        return AgentEmission(
            emission_id=stable or new_emission_id(),
            session_id=session,
            cycle_id=cycle,
            generation=0,
            context_revision_id=new_context_revision_id(),
            kind="intermediate",
            text=f"message:{session}:{cycle}",
            response_route={"client": "test"},
            idempotency_key=f"emission:{session}:{cycle}",
            created_at=NOW,
        )
    if kind == "finalization":
        return CycleFinalizationRecord(
            finalization_id=stable or new_finalization_id(),
            session_id=session,
            cycle_id=cycle,
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


def repository(bundle, kind):
    return getattr(
        bundle,
        {
            "admission": "admissions",
            "inbox": "inbox",
            "control": "controls",
            "snapshot": "snapshots",
            "context": "context_revisions",
            "emission": "emissions",
            "finalization": "finalizations",
        }[kind],
    )


async def create(bundle, kind, record):
    repo = repository(bundle, kind)
    if kind in {"admission", "inbox", "snapshot", "emission"}:
        return await repo.create_if_absent(record)
    if kind == "control":
        return await repo.append(record)
    if kind == "context":
        return await repo.append_revision(record)
    if kind == "finalization":
        return await repo.prepare(record)
    raise AssertionError(kind)


def stable_id(kind, record):
    field = {
        "admission": "admission_id",
        "inbox": "inbox_item_id",
        "control": "control_id",
        "snapshot": "cycle_id",
        "context": "context_revision_id",
        "emission": "emission_id",
        "finalization": "finalization_id",
    }[kind]
    return getattr(record, field)


def cycle_id(kind, record):
    return (
        record.target_cycle_id
        if kind in {"admission", "control"}
        else record.cycle_id
    )


def record_path(bundle, kind, record):
    layout = repository(bundle, kind).layout
    if kind == "admission":
        return layout.admission(record.session_id, record.admission_id)
    if kind == "inbox":
        return layout.inbox_item(record.cycle_id, record.inbox_item_id)
    if kind == "control":
        return layout.control(record.session_id, record.control_id)
    if kind == "snapshot":
        return layout.snapshot(record.cycle_id)
    if kind == "context":
        return layout.revision(record.cycle_id, record.context_revision_id)
    if kind == "emission":
        return layout.emission(record.cycle_id, record.emission_id)
    return layout.finalization(record.cycle_id, record.finalization_id)


def index_paths(bundle, kind, record):
    layout = repository(bundle, kind).layout
    authority = layout.cycle_authority(cycle_id(kind, record))
    if kind == "admission":
        return (
            layout.record_index("admission", record.admission_id),
            layout.admission_input(record.input_batch_id),
            authority,
        )
    if kind == "inbox":
        return (
            layout.record_index("inbox", record.inbox_item_id),
            layout.inbox_admission(record.admission_id),
            layout.inbox_input(record.input_batch_id),
            authority,
        )
    if kind == "control":
        return (layout.record_index("control", record.control_id), authority)
    if kind == "snapshot":
        return (layout.record_index("snapshot", record.cycle_id), authority)
    if kind == "context":
        return (
            layout.record_index("revision", record.context_revision_id),
            authority,
        )
    if kind == "emission":
        return (layout.record_index("emission", record.emission_id), authority)
    return (
        layout.record_index("finalization", record.finalization_id),
        authority,
    )


def durable_records(bundle, kind):
    root = repository(bundle, kind).layout.root
    model, pattern = {
        "admission": (InputAdmissionRecord, "sessions/*/admissions/*.json"),
        "inbox": (CycleInboxItem, "cycles/*/inbox/*.json"),
        "control": (SessionControlCommand, "sessions/*/controls/*.json"),
        "snapshot": (ActiveCycleSnapshot, "cycles/*/snapshot.json"),
        "context": (CycleContextRevision, "cycles/*/context-revisions/*.json"),
        "emission": (AgentEmission, "cycles/*/emissions/*.json"),
        "finalization": (
            CycleFinalizationRecord,
            "cycles/*/finalizations/*.json",
        ),
    }[kind]
    return tuple(read_model(path, model) for path in root.glob(pattern))


def count_stable(bundle, kind, record):
    expected = stable_id(kind, record)
    return sum(stable_id(kind, item) == expected for item in durable_records(bundle, kind))


def competing(kind, record):
    cycle = cycle_id(kind, record) if kind == "snapshot" else f"other:{cycle_id(kind, record)}"
    return make(kind, "session-2", cycle, stable_id(kind, record))


def assert_rebuilt(bundle, kind, record):
    repo = repository(bundle, kind)
    authority = repo.layout.cycle_authority(cycle_id(kind, record))
    for path in index_paths(bundle, kind, record):
        assert path.exists(), path
        pointer = repo._read_pointer(path)
        assert pointer is not None
        if path == authority:
            assert pointer.session_id == record.session_id
        else:
            assert repo._pointer_record_path(pointer).exists()


def fail_index_write(monkeypatch, number):
    real = common_module.atomic_write_model
    calls = 0

    def crashing(path, model):
        nonlocal calls
        calls += 1
        if calls == number:
            raise RuntimeError(f"index crash {number}")
        return real(path, model)

    monkeypatch.setattr(common_module, "atomic_write_model", crashing)
    return real


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("fail_at", (1, 2))
async def test_record_first_index_crash_recovers_before_competing_create(
    tmp_path,
    monkeypatch,
    kind,
    fail_at,
):
    first = repositories(tmp_path)
    record = make(kind, "session-1", "cycle-1")
    real = fail_index_write(monkeypatch, fail_at)

    with pytest.raises(RuntimeError, match=f"index crash {fail_at}"):
        await create(first, kind, record)

    assert record_path(first, kind, record).exists()
    assert count_stable(first, kind, record) == 1
    if fail_at == 1:
        assert not any(path.exists() for path in index_paths(first, kind, record))
    else:
        existing = [path.exists() for path in index_paths(first, kind, record)]
        assert any(existing) and not all(existing)
    monkeypatch.setattr(common_module, "atomic_write_model", real)

    same_bundle = repositories(tmp_path)
    competing_bundle = repositories(tmp_path)
    same, rival = await asyncio.gather(
        create(same_bundle, kind, record),
        create(competing_bundle, kind, competing(kind, record)),
        return_exceptions=True,
    )

    assert same == record
    assert isinstance(rival, InputRuntimeConflictError)
    assert count_stable(same_bundle, kind, record) == 1
    assert_rebuilt(same_bundle, kind, record)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", KINDS)
async def test_dangling_stable_pointer_without_record_is_replaced(tmp_path, kind):
    bundle = repositories(tmp_path)
    record = make(kind, "session-1", "cycle-1")
    repo = repository(bundle, kind)
    index = index_paths(bundle, kind, record)[0]
    repo._write_pointer(
        index,
        repo._pointer(
            kind,
            stable_id(kind, record),
            "ghost-session",
            record_path(bundle, kind, record),
            cycle_id(kind, record),
        ),
    )

    assert await create(bundle, kind, record) == record
    assert count_stable(bundle, kind, record) == 1
    assert_rebuilt(bundle, kind, record)


@pytest.mark.asyncio
async def test_lost_admission_input_index_blocks_competing_session(tmp_path):
    first = repositories(tmp_path)
    record = make("admission", "session-1", "cycle-1")
    await create(first, "admission", record)
    relation = first.admissions.layout.admission_input(record.input_batch_id)
    relation.unlink()

    rival = make("admission", "session-2", "cycle-2").model_copy(
        update={"input_batch_id": record.input_batch_id}
    )
    restarted = repositories(tmp_path)
    with pytest.raises(InputRuntimeConflictError, match="relation changed"):
        await create(restarted, "admission", rival)

    assert relation.exists()
    assert count_stable(restarted, "admission", record) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ("admission", "input"))
async def test_lost_inbox_relation_index_blocks_competing_session(
    tmp_path,
    identity,
):
    first = repositories(tmp_path)
    record = make("inbox", "session-1", "cycle-1")
    await create(first, "inbox", record)
    relation = (
        first.inbox.layout.inbox_admission(record.admission_id)
        if identity == "admission"
        else first.inbox.layout.inbox_input(record.input_batch_id)
    )
    relation.unlink()

    updates = (
        {"admission_id": record.admission_id}
        if identity == "admission"
        else {"input_batch_id": record.input_batch_id}
    )
    rival = make("inbox", "session-2", "cycle-2").model_copy(update=updates)
    restarted = repositories(tmp_path)
    with pytest.raises(InputRuntimeConflictError, match="identity conflict"):
        await create(restarted, "inbox", rival)

    assert relation.exists()
    assert count_stable(restarted, "inbox", record) == 1


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
    kind = "admission" if identity == "admission_input" else "inbox"
    record = make(kind, "session-1", "cycle-1")
    repo = repository(bundle, kind)
    if identity == "admission_input":
        index = repo.layout.admission_input(record.input_batch_id)
    elif identity == "inbox_admission":
        index = repo.layout.inbox_admission(record.admission_id)
    else:
        index = repo.layout.inbox_input(record.input_batch_id)
    repo._write_pointer(
        index,
        repo._pointer(
            "dangling",
            "missing",
            "ghost-session",
            record_path(bundle, kind, record),
            cycle_id(kind, record),
        ),
    )

    assert await create(bundle, kind, record) == record
    assert count_stable(bundle, kind, record) == 1
    assert_rebuilt(bundle, kind, record)


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
    existing = make(existing_kind, "session-1", "shared-cycle")
    await create(first, existing_kind, existing)
    authority = repository(first, existing_kind).layout.cycle_authority(
        "shared-cycle"
    )
    authority.unlink()

    restarted = repositories(tmp_path)
    rival = make(competing_kind, "session-2", "shared-cycle")
    with pytest.raises(InputRuntimeConflictError, match="another session"):
        await create(restarted, competing_kind, rival)

    pointer = repository(restarted, existing_kind)._read_pointer(authority)
    assert pointer is not None and pointer.session_id == "session-1"
    assert count_stable(restarted, existing_kind, existing) == 1
    assert not record_path(restarted, competing_kind, rival).exists()


@pytest.mark.asyncio
async def test_dangling_cycle_authority_without_record_is_replaced(tmp_path):
    bundle = repositories(tmp_path)
    record = make("snapshot", "session-1", "cycle-1")
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

    assert await create(bundle, "snapshot", record) == record
    pointer = repo._read_pointer(authority)
    assert pointer is not None and pointer.session_id == record.session_id
    assert count_stable(bundle, "snapshot", record) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind",
    ("admission", "inbox", "control", "context", "emission", "finalization"),
)
async def test_ambiguous_durable_stable_identity_is_consistency_error(
    tmp_path,
    kind,
):
    bundle = repositories(tmp_path)
    first = make(kind, "session-1", "cycle-1")
    second = make(kind, "session-2", "cycle-2", stable_id(kind, first))
    atomic_write_model(record_path(bundle, kind, first), first)
    atomic_write_model(record_path(bundle, kind, second), second)

    with pytest.raises(InputRuntimeConflictError, match="ambiguous authoritative"):
        await create(
            repositories(tmp_path),
            kind,
            make(kind, "session-3", "cycle-3", stable_id(kind, first)),
        )
