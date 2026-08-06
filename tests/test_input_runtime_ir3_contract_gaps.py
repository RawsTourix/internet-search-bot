from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from src.input_runtime import (
    InputAdmissionAction,
    InputAdmissionService,
    InputRuntimeConfigType,
    InputRuntimeRepositories,
    create_filesystem_input_runtime_repositories,
)
from src.runtime import SessionExecutionCoordinator
from src.storage import StorageConfigType


NOW = datetime(2026, 8, 6, tzinfo=timezone.utc)


@dataclass
class Batch:
    input_batch_id: str
    session_id: str = "session"
    payload_size: int = 1
    source_event_ids: tuple[str, ...] = ("evt_" + "1" * 32,)
    content_fingerprint: str = "sha256:" + "2" * 64
    committed_at: datetime = NOW

    def model_dump_json(self) -> str:
        return "x" * self.payload_size


class Reader:
    def __init__(self, *batches: Batch):
        self.batches = {item.input_batch_id: item for item in batches}

    async def get_committed(self, input_batch_id: str) -> Batch:
        return self.batches[input_batch_id]


class Wake:
    async def wake(self, session_id: str, *, cycle_id: str) -> bool:
        return True


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


def repositories(tmp_path):
    return create_filesystem_input_runtime_repositories(
        storage_config=StorageConfigType(root_dir=str(tmp_path))
    )


def flaky_repositories(base):
    return InputRuntimeRepositories(
        sessions=base.sessions,
        admissions=base.admissions,
        inbox=FailOnceInbox(base.inbox),
        controls=base.controls,
        snapshots=base.snapshots,
        context_revisions=base.context_revisions,
        emissions=base.emissions,
        finalizations=base.finalizations,
        coordination_root=base.coordination_root,
        coordination_locks=base.coordination_locks,
    )


def service(tmp_path, batches, *, config, repository_bundle):
    return InputAdmissionService(
        config=config,
        repositories=repository_bundle,
        committed_batches=Reader(*batches),
        wake_coordinator=Wake(),
        cycle_id_factory=lambda: "cycle-a",
        clock=lambda: NOW,
        payload_size_resolver=lambda batch: batch.payload_size,
    )


@pytest.mark.asyncio
async def test_missing_inbox_admission_reserves_count_capacity(tmp_path):
    batches = [Batch("initial"), Batch("addition-a"), Batch("batch-b")]
    config = InputRuntimeConfigType(
        max_queued_batches_per_session=1,
        max_queued_bytes_per_session=100,
        max_batches_per_checkpoint=1,
        max_batch_bytes_per_checkpoint=100,
    )
    base = repositories(tmp_path)
    svc = service(
        tmp_path,
        batches,
        config=config,
        repository_bundle=flaky_repositories(base),
    )
    await svc.admit_committed_batch("initial", session_id="session")

    with pytest.raises(OSError):
        await svc.admit_committed_batch("addition-a", session_id="session")
    assert await base.inbox.list_for_cycle("cycle-a") == ()

    blocked = await svc.admit_committed_batch("batch-b", session_id="session")
    assert blocked.action == InputAdmissionAction.CAPACITY_BLOCKED
    assert blocked.reason_code == "max_queued_batches_per_session"
    assert await base.admissions.get_by_input_batch_id("batch-b") is None

    repaired = await svc.admit_committed_batch(
        "addition-a",
        session_id="session",
    )
    assert repaired.action == InputAdmissionAction.DUPLICATE
    inbox = await base.inbox.list_for_cycle("cycle-a")
    assert len(inbox) == 1
    assert inbox[0].input_batch_id == "addition-a"
    assert inbox[0].cycle_sequence == 1


@pytest.mark.asyncio
async def test_missing_inbox_admission_reserves_byte_capacity(tmp_path):
    batches = [
        Batch("initial", payload_size=1),
        Batch("addition-a", payload_size=7),
        Batch("batch-b", payload_size=4),
    ]
    config = InputRuntimeConfigType(
        max_queued_batches_per_session=4,
        max_queued_bytes_per_session=10,
        max_batches_per_checkpoint=4,
        max_batch_bytes_per_checkpoint=10,
    )
    base = repositories(tmp_path)
    svc = service(
        tmp_path,
        batches,
        config=config,
        repository_bundle=flaky_repositories(base),
    )
    await svc.admit_committed_batch("initial", session_id="session")

    with pytest.raises(OSError):
        await svc.admit_committed_batch("addition-a", session_id="session")
    assert await base.inbox.list_for_cycle("cycle-a") == ()

    blocked = await svc.admit_committed_batch("batch-b", session_id="session")
    assert blocked.action == InputAdmissionAction.CAPACITY_BLOCKED
    assert blocked.reason_code == "max_queued_bytes_per_session"

    repaired = await svc.admit_committed_batch(
        "addition-a",
        session_id="session",
    )
    assert repaired.action == InputAdmissionAction.DUPLICATE
    inbox = await base.inbox.list_for_cycle("cycle-a")
    assert len(inbox) == 1
    assert inbox[0].payload_size_bytes == 7


@pytest.mark.asyncio
async def test_late_wake_for_old_cycle_does_not_wake_new_cycle():
    coordinator = SessionExecutionCoordinator()
    async with coordinator.admitted_run_lease(
        session_id="session",
        input_batch_id="new-input",
        cycle_id="new-cycle",
    ) as acquired:
        assert acquired is True
        assert await coordinator.wake(
            "session",
            cycle_id="old-cycle",
        ) is False
        assert await coordinator.wait_for_wakeup(
            "session",
            timeout=0.01,
        ) is False
        assert await coordinator.wake(
            "session",
            cycle_id="new-cycle",
        ) is True
        assert await coordinator.wait_for_wakeup(
            "session",
            timeout=0.01,
        ) is True
