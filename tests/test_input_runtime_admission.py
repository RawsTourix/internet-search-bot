from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.input_runtime import (
    CycleStatus,
    InputAdmissionAction,
    InputAdmissionService,
    InputRuntimeConfigType,
    InputRuntimeRepositories,
    SessionInputRuntimeState,
    create_filesystem_input_runtime_repositories,
    new_finalization_id,
)
from src.input_runtime.errors import InputRuntimeConflictError
from src.storage import StorageConfigType


NOW = datetime(2026, 8, 6, tzinfo=timezone.utc)


@dataclass
class FakeBatch:
    input_batch_id: str
    session_id: str
    payload_size: int = 1
    source_event_ids: tuple[str, ...] = ("evt_" + "1" * 32,)
    content_fingerprint: str = "sha256:" + "2" * 64
    committed_at: datetime = NOW

    def model_dump_json(self) -> str:
        return "x" * self.payload_size


class FakeReader:
    def __init__(self, *batches: FakeBatch):
        self.batches = {item.input_batch_id: item for item in batches}

    async def get_committed(self, input_batch_id: str) -> FakeBatch:
        return self.batches[input_batch_id]


class FakeWakeCoordinator:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def wake(self, session_id: str, *, cycle_id: str) -> bool:
        self.calls.append((session_id, cycle_id))
        return True


def repositories(tmp_path):
    return create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )


def service(
    tmp_path,
    *batches: FakeBatch,
    config: InputRuntimeConfigType | None = None,
    cycle_ids=None,
    repository_bundle=None,
):
    ids = iter(cycle_ids or [f"cycle-{index}" for index in range(20)])
    wake = FakeWakeCoordinator()
    result = InputAdmissionService(
        config=config or InputRuntimeConfigType(),
        repositories=repository_bundle or repositories(tmp_path),
        committed_batches=FakeReader(*batches),
        wake_coordinator=wake,
        cycle_id_factory=lambda: next(ids),
        clock=lambda: NOW,
        payload_size_resolver=lambda batch: batch.payload_size,
    )
    return result, wake


async def set_status(repos, session_id: str, status: CycleStatus):
    state = await repos.sessions.get(session_id)
    assert state is not None
    updates = {
        "cycle_status": status,
        "revision": state.revision + 1,
        "updated_at": NOW,
    }
    if status == CycleStatus.FINALIZING:
        updates["finalization_id"] = new_finalization_id()
    else:
        updates["finalization_id"] = None
    replacement = SessionInputRuntimeState.model_validate(
        state.model_copy(update=updates).model_dump(mode="python")
    )
    return await repos.sessions.compare_and_swap(state.revision, replacement)


@pytest.mark.asyncio
async def test_basic_admission_decision_matrix_and_sequences(tmp_path):
    batches = [FakeBatch(f"batch-{index}", "session") for index in range(10)]
    svc, wake = service(tmp_path, *batches, cycle_ids=["cycle-a", "cycle-b"])

    initial = await svc.admit_committed_batch("batch-0", session_id="session")
    assert initial.action == InputAdmissionAction.START_CYCLE
    assert initial.cycle_sequence == 0
    assert initial.session_sequence == 1
    assert initial.should_start_runner is True

    running = await svc.admit_committed_batch("batch-1", session_id="session")
    assert running.action == InputAdmissionAction.QUEUED_RUNNING
    assert running.cycle_sequence == 1
    assert running.target_cycle_id == initial.target_cycle_id
    assert running.should_start_runner is False
    assert running.should_wake_runner is True

    second = await svc.admit_committed_batch("batch-2", session_id="session")
    third = await svc.admit_committed_batch("batch-3", session_id="session")
    assert [second.cycle_sequence, third.cycle_sequence] == [2, 3]
    assert [second.session_sequence, third.session_sequence] == [3, 4]

    await set_status(svc.repositories, "session", CycleStatus.WAITING_USER)
    waiting = await svc.admit_committed_batch("batch-4", session_id="session")
    assert waiting.action == InputAdmissionAction.RESUME_WAITING

    await set_status(svc.repositories, "session", CycleStatus.PAUSE_REQUESTED)
    paused = await svc.admit_committed_batch("batch-5", session_id="session")
    assert paused.action == InputAdmissionAction.QUEUED_PAUSED
    assert paused.should_wake_runner is False

    await set_status(svc.repositories, "session", CycleStatus.INTERRUPTED)
    interrupted = await svc.admit_committed_batch("batch-6", session_id="session")
    assert interrupted.action == InputAdmissionAction.RESUME_INTERRUPTED

    await set_status(svc.repositories, "session", CycleStatus.FINALIZING)
    finalizing = await svc.admit_committed_batch("batch-7", session_id="session")
    assert finalizing.action == InputAdmissionAction.QUEUED_RUNNING
    assert finalizing.target_cycle_id == initial.target_cycle_id

    assert len(await svc.repositories.inbox.list_for_cycle("cycle-a")) == 7
    assert wake.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status",
    [CycleStatus.DONE, CycleStatus.ERROR, CycleStatus.CANCELLED],
)
async def test_terminal_state_allocates_a_new_cycle(tmp_path, terminal_status):
    first = FakeBatch("batch-1", "session")
    second = FakeBatch("batch-2", "session")
    svc, _ = service(
        tmp_path,
        first,
        second,
        cycle_ids=["cycle-a", "cycle-b"],
    )
    initial = await svc.admit_committed_batch("batch-1", session_id="session")
    await set_status(svc.repositories, "session", terminal_status)
    next_cycle = await svc.admit_committed_batch("batch-2", session_id="session")
    assert next_cycle.action == InputAdmissionAction.START_CYCLE
    assert next_cycle.target_cycle_id == "cycle-b"
    assert next_cycle.target_cycle_id != initial.target_cycle_id
    assert next_cycle.cycle_sequence == 0


@pytest.mark.asyncio
async def test_duplicate_is_restart_safe_and_repairs_one_inbox(tmp_path):
    batches = [FakeBatch("initial", "session"), FakeBatch("addition", "session")]
    svc, _ = service(tmp_path, *batches, cycle_ids=["cycle-a"])
    await svc.admit_committed_batch("initial", session_id="session")
    admitted = await svc.admit_committed_batch("addition", session_id="session")

    restarted, _ = service(
        tmp_path,
        *batches,
        cycle_ids=["unused"],
        repository_bundle=repositories(tmp_path),
    )
    duplicate = await restarted.admit_committed_batch(
        "addition",
        session_id="session",
    )
    assert duplicate.action == InputAdmissionAction.DUPLICATE
    assert duplicate.admission_id == admitted.admission_id
    assert duplicate.cycle_sequence == admitted.cycle_sequence
    inbox = await restarted.repositories.inbox.list_for_cycle("cycle-a")
    assert len(inbox) == 1
    assert inbox[0].admission_id == admitted.admission_id


class FailOnceInbox:
    def __init__(self, delegate):
        self.delegate = delegate
        self.failed = False

    async def create_if_absent(self, item):
        if not self.failed:
            self.failed = True
            raise OSError("simulated inbox publication failure")
        return await self.delegate.create_if_absent(item)

    def __getattr__(self, name):
        return getattr(self.delegate, name)


@pytest.mark.asyncio
async def test_crash_after_admission_repairs_inbox_without_new_sequence(tmp_path):
    initial = FakeBatch("initial", "session")
    addition = FakeBatch("addition", "session")
    base = repositories(tmp_path)
    flaky = InputRuntimeRepositories(
        sessions=base.sessions,
        admissions=base.admissions,
        inbox=FailOnceInbox(base.inbox),
        controls=base.controls,
        snapshots=base.snapshots,
        context_revisions=base.context_revisions,
        emissions=base.emissions,
        finalizations=base.finalizations,
    )
    svc, _ = service(
        tmp_path,
        initial,
        addition,
        cycle_ids=["cycle-a"],
        repository_bundle=flaky,
    )
    await svc.admit_committed_batch("initial", session_id="session")
    with pytest.raises(OSError):
        await svc.admit_committed_batch("addition", session_id="session")

    persisted = await base.admissions.get_by_input_batch_id("addition")
    assert persisted is not None
    assert persisted.cycle_sequence == 1
    assert await base.inbox.list_for_cycle("cycle-a") == ()

    repaired = await svc.admit_committed_batch("addition", session_id="session")
    assert repaired.action == InputAdmissionAction.DUPLICATE
    assert repaired.admission_id == persisted.admission_id
    inbox = await base.inbox.list_for_cycle("cycle-a")
    assert len(inbox) == 1
    assert inbox[0].cycle_sequence == 1


@pytest.mark.asyncio
async def test_capacity_count_and_bytes_are_typed_and_recoverable(tmp_path):
    batches = [
        FakeBatch("initial", "session", 1),
        FakeBatch("one", "session", 6),
        FakeBatch("blocked", "session", 5),
    ]
    config = InputRuntimeConfigType(
        max_queued_batches_per_session=1,
        max_queued_bytes_per_session=10,
        max_batches_per_checkpoint=1,
        max_batch_bytes_per_checkpoint=10,
    )
    svc, _ = service(
        tmp_path,
        *batches,
        config=config,
        cycle_ids=["cycle-a"],
    )
    await svc.admit_committed_batch("initial", session_id="session")
    await svc.admit_committed_batch("one", session_id="session")
    blocked = await svc.admit_committed_batch("blocked", session_id="session")
    assert blocked.action == InputAdmissionAction.CAPACITY_BLOCKED
    assert blocked.retryable is True
    assert blocked.reason_code == "max_queued_batches_per_session"
    assert (
        await svc.repositories.admissions.get_by_input_batch_id("blocked")
        is None
    )
    assert svc.committed_batches.batches["blocked"].input_batch_id == "blocked"

    await svc.repositories.inbox.cancel_generation(
        "session",
        generation=0,
        cancelled_at=NOW,
        reason_code="capacity_test_release",
    )
    admitted = await svc.admit_committed_batch("blocked", session_id="session")
    assert admitted.action == InputAdmissionAction.QUEUED_RUNNING


@pytest.mark.asyncio
async def test_byte_capacity_reason_when_count_has_room(tmp_path):
    batches = [
        FakeBatch("initial", "session", 1),
        FakeBatch("one", "session", 7),
        FakeBatch("blocked", "session", 4),
    ]
    config = InputRuntimeConfigType(
        max_queued_batches_per_session=4,
        max_queued_bytes_per_session=10,
        max_batches_per_checkpoint=4,
        max_batch_bytes_per_checkpoint=10,
    )
    svc, _ = service(
        tmp_path,
        *batches,
        config=config,
        cycle_ids=["cycle-a"],
    )
    await svc.admit_committed_batch("initial", session_id="session")
    await svc.admit_committed_batch("one", session_id="session")
    blocked = await svc.admit_committed_batch("blocked", session_id="session")
    assert blocked.reason_code == "max_queued_bytes_per_session"


@pytest.mark.asyncio
async def test_unadmitted_committed_batch_is_discoverable_for_reconciliation(tmp_path):
    initial = FakeBatch("initial", "session")
    missing = FakeBatch("missing", "session")
    svc, _ = service(tmp_path, initial, missing, cycle_ids=["cycle-a"])
    await svc.admit_committed_batch("initial", session_id="session")
    found = await svc.find_committed_without_admission(["initial", "missing"])
    assert found == (missing,)
    reconciled = await svc.reconcile_committed_batch(
        "missing",
        session_id="session",
    )
    assert reconciled.action == InputAdmissionAction.QUEUED_RUNNING


@pytest.mark.asyncio
async def test_session_mismatch_and_cycle_authority_conflict_are_managed(tmp_path):
    wrong = FakeBatch("batch", "owner")
    svc, _ = service(tmp_path, wrong, cycle_ids=["shared-cycle"])
    with pytest.raises(InputRuntimeConflictError):
        await svc.admit_committed_batch("batch", session_id="other")

    first = FakeBatch("first", "session-a")
    second = FakeBatch("second", "session-b")
    repos = repositories(tmp_path / "authority")
    svc_a, _ = service(
        tmp_path / "authority",
        first,
        second,
        cycle_ids=["shared-cycle", "shared-cycle"],
        repository_bundle=repos,
    )
    await svc_a.admit_committed_batch("first", session_id="session-a")
    with pytest.raises(InputRuntimeConflictError):
        await svc_a.admit_committed_batch("second", session_id="session-b")

    shared_a = FakeBatch("shared", "session-a")
    shared_b = FakeBatch("shared", "session-b")
    shared_repos = repositories(tmp_path / "shared-input")
    shared_service_a, _ = service(
        tmp_path / "shared-input",
        shared_a,
        cycle_ids=["cycle-a"],
        repository_bundle=shared_repos,
    )
    await shared_service_a.admit_committed_batch("shared", session_id="session-a")
    shared_service_b, _ = service(
        tmp_path / "shared-input",
        shared_b,
        cycle_ids=["cycle-b"],
        repository_bundle=shared_repos,
    )
    with pytest.raises(InputRuntimeConflictError):
        await shared_service_b.admit_committed_batch(
            "shared",
            session_id="session-b",
        )


@pytest.mark.asyncio
async def test_concurrent_idle_batches_allocate_one_cycle_and_one_fifo_addition(tmp_path):
    first = FakeBatch("first", "session")
    second = FakeBatch("second", "session")
    svc, _ = service(
        tmp_path,
        first,
        second,
        cycle_ids=["cycle-a", "unused-cycle"],
    )
    outcomes = await asyncio.gather(
        svc.admit_committed_batch("first", session_id="session"),
        svc.admit_committed_batch("second", session_id="session"),
    )
    ordered = sorted(outcomes, key=lambda item: item.session_sequence or 0)
    assert [item.action for item in ordered] == [
        InputAdmissionAction.START_CYCLE,
        InputAdmissionAction.QUEUED_RUNNING,
    ]
    assert [item.cycle_sequence for item in ordered] == [0, 1]
    assert {item.target_cycle_id for item in outcomes} == {"cycle-a"}


@pytest.mark.asyncio
async def test_concurrent_capacity_check_admits_only_one_pending_batch(tmp_path):
    batches = [
        FakeBatch("initial", "session", 1),
        FakeBatch("one", "session", 1),
        FakeBatch("two", "session", 1),
    ]
    config = InputRuntimeConfigType(
        max_queued_batches_per_session=1,
        max_queued_bytes_per_session=10,
        max_batches_per_checkpoint=1,
        max_batch_bytes_per_checkpoint=10,
    )
    svc, _ = service(
        tmp_path,
        *batches,
        config=config,
        cycle_ids=["cycle-a"],
    )
    await svc.admit_committed_batch("initial", session_id="session")
    outcomes = await asyncio.gather(
        svc.admit_committed_batch("one", session_id="session"),
        svc.admit_committed_batch("two", session_id="session"),
    )
    assert sorted(item.action.value for item in outcomes) == [
        "capacity_blocked",
        "queued_running",
    ]
    assert len(await svc.repositories.inbox.list_for_cycle("cycle-a")) == 1
