from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.input_runtime import (
    CheckpointAction,
    CheckpointName,
    CycleInputApplier,
    CycleStatus,
    InboxState,
    InputAdmissionService,
    InputRuntimeConfigType,
    create_filesystem_input_runtime_repositories,
)
from src.input_runtime.recovery import InputRuntimeReadinessGate
from src.input_runtime.recovery_hardening import InputRuntimeRecoveryCoordinator
from src.runtime import ActiveAgentCycle, SessionExecutionCoordinator
from src.storage import StorageConfigType

NOW = datetime(2026, 8, 8, 19, 15, tzinfo=timezone.utc)


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


def runtime_service(tmp_path, reader, *, cycle_factory=lambda: "cycle"):
    repositories = bundle(tmp_path)
    coordinator = SessionExecutionCoordinator()
    service = InputAdmissionService(
        config=InputRuntimeConfigType(claim_lease_seconds=1),
        repositories=repositories,
        committed_batches=reader,
        wake_coordinator=coordinator,
        cycle_id_factory=cycle_factory,
        clock=lambda: NOW,
        payload_size_resolver=lambda batch: batch.payload_size,
    )
    return repositories, coordinator, service


def active_cycle() -> ActiveAgentCycle:
    return ActiveAgentCycle(
        cycle_id="cycle",
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


async def seed(tmp_path, *, additions=1):
    batches = [Batch("initial", 1)] + [
        Batch(f"add-{index}", index + 2)
        for index in range(additions)
    ]
    reader = Reader(*batches)
    repositories, coordinator, service = runtime_service(tmp_path, reader)
    initial = await service.admit_committed_batch("initial", session_id="session")
    cycle = active_cycle()
    applier = CycleInputApplier(
        config=service.config,
        repositories=repositories,
        committed_batches=reader,
        clock=lambda: NOW,
    )
    outcome = await applier.ensure_initial_context(
        session_id="session",
        cycle_id=initial.target_cycle_id,
        generation=0,
        checkpoint=CheckpointName.RESUME,
        active_cycle=cycle,
        input_batch_id="initial",
    )
    assert outcome.action == CheckpointAction.CONTINUE
    for index in range(additions):
        await service.admit_committed_batch(
            f"add-{index}",
            session_id="session",
        )
    return reader, repositories, coordinator, service, cycle


async def recover(tmp_path, reader, *, now):
    fresh, coordinator, service = runtime_service(
        tmp_path,
        reader,
        cycle_factory=lambda: "must-not-create",
    )
    gate = InputRuntimeReadinessGate()
    recovery = InputRuntimeRecoveryCoordinator(
        repositories=fresh,
        admission_service=service,
        committed_batches=reader,
        readiness_gate=gate,
        generation_coordinator=coordinator,
        clock=lambda: now,
    )
    plan = await recovery.recover()
    return fresh, gate, plan


@pytest.mark.asyncio
async def test_expired_claimed_range_requeues_without_semantic_apply(tmp_path):
    reader, repositories, _, _, _ = await seed(tmp_path, additions=2)
    claim = await repositories.inbox.claim_contiguous_range(
        "cycle",
        generation=0,
        after_sequence=0,
        max_items=10,
        max_bytes=10_000,
        lease_seconds=1,
    )
    assert claim is not None
    assert (claim.first_cycle_sequence, claim.last_cycle_sequence) == (1, 2)
    before_snapshot = await repositories.snapshots.get("cycle")
    before_revisions = await repositories.context_revisions.list_for_cycle("cycle")

    fresh, _, plan = await recover(
        tmp_path,
        reader,
        now=claim.claim_expires_at + timedelta(seconds=1),
    )
    items = await fresh.inbox.list_for_cycle("cycle")
    assert [item.state for item in items] == [InboxState.QUEUED, InboxState.QUEUED]
    snapshot = await fresh.snapshots.get("cycle")
    revisions = await fresh.context_revisions.list_for_cycle("cycle")
    assert snapshot.active_context_revision_id == before_snapshot.active_context_revision_id
    assert snapshot.applied_through_cycle_sequence == 0
    assert revisions == before_revisions
    assert plan.report.inbox_claims_reconciled == 1


@pytest.mark.asyncio
async def test_expired_applying_snapshot_lower_requeues_whole_range(tmp_path):
    reader, repositories, _, _, _ = await seed(tmp_path, additions=2)
    claim = await repositories.inbox.claim_contiguous_range(
        "cycle",
        generation=0,
        after_sequence=0,
        max_items=10,
        max_bytes=10_000,
        lease_seconds=1,
    )
    assert claim is not None
    await repositories.inbox.mark_applying(claim)

    fresh, _, _ = await recover(
        tmp_path,
        reader,
        now=claim.claim_expires_at + timedelta(seconds=1),
    )
    items = await fresh.inbox.list_for_cycle("cycle")
    assert [item.state for item in items] == [InboxState.QUEUED, InboxState.QUEUED]
    assert (await fresh.snapshots.get("cycle")).applied_through_cycle_sequence == 0
    admissions = await fresh.admissions.list_for_session("session")
    additions = [item for item in admissions if item.cycle_sequence > 0]
    assert [item.state.value for item in additions] == ["admitted", "admitted"]


@pytest.mark.asyncio
async def test_expired_applying_snapshot_applied_finishes_markers_without_duplicate_revision(
    tmp_path,
    monkeypatch,
):
    reader, repositories, _, service, cycle = await seed(tmp_path, additions=2)
    original_mark_applied = repositories.inbox.mark_applied
    persisted = pytest.MonkeyPatch()

    async def fail_after_snapshot(*args, **kwargs):
        raise OSError("crash after snapshot commit")

    monkeypatch.setattr(repositories.inbox, "mark_applied", fail_after_snapshot)
    with pytest.raises(OSError, match="crash after snapshot commit"):
        await service.cycle_input_applier.apply_pending_input(
            session_id="session",
            cycle_id="cycle",
            generation=0,
            checkpoint=CheckpointName.BEFORE_LLM,
            active_cycle=cycle,
            through_sequence=2,
        )
    monkeypatch.setattr(repositories.inbox, "mark_applied", original_mark_applied)

    crashed_items = await repositories.inbox.list_for_cycle("cycle")
    assert [item.state for item in crashed_items] == [InboxState.APPLYING, InboxState.APPLYING]
    snapshot = await repositories.snapshots.get("cycle")
    assert snapshot.applied_through_cycle_sequence == 2
    assert snapshot.applied_input_batch_ids == ["initial", "add-0", "add-1"]
    revisions_before = await repositories.context_revisions.list_for_cycle("cycle")
    assert len(revisions_before) == 2
    expires_at = crashed_items[0].claim_expires_at
    assert expires_at is not None

    fresh, _, _ = await recover(
        tmp_path,
        reader,
        now=expires_at + timedelta(seconds=1),
    )
    items = await fresh.inbox.list_for_cycle("cycle")
    assert [item.state for item in items] == [InboxState.APPLIED, InboxState.APPLIED]
    admissions = await fresh.admissions.list_for_session("session")
    assert [item.state.value for item in admissions] == ["applied", "applied", "applied"]
    state = await fresh.sessions.get("session")
    assert state.active_cycle_applied_through_sequence == 2
    assert state.active_context_revision_id == snapshot.active_context_revision_id
    revisions_after = await fresh.context_revisions.list_for_cycle("cycle")
    assert revisions_after == revisions_before
