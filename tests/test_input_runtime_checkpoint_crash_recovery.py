from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from src.input_runtime import (
    ActiveCycleSnapshot,
    CheckpointName,
    CycleContextRevision,
    CycleStatus,
    InputAdmissionService,
    InputRuntimeConfigType,
    InputRuntimeRepositories,
    build_input_batch_update,
    build_input_batch_update_message,
    create_filesystem_input_runtime_repositories,
)
from src.runtime import ActiveAgentCycle
from src.storage import StorageConfigType
NOW = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)

@dataclass
class Batch:
    input_batch_id: str
    session_id: str = 'session'
    payload_size: int = 10
    text_parts: list[object] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    source_event_ids: tuple[str, ...] = ('evt_' + '1' * 32,)
    content_fingerprint: str = 'sha256:' + '2' * 64
    committed_at: datetime = NOW
    continuation_of_batch_id: str | None = None
    correction_of_batch_id: str | None = None
    artifact_manifest: object = field(default_factory=lambda: SimpleNamespace(items=()))
    def model_dump_json(self) -> str:
        return 'x' * self.payload_size

class Reader:
    def __init__(self, *batches):
        self.batches = {item.input_batch_id: item for item in batches}
    async def get_committed(self, input_batch_id):
        return self.batches[input_batch_id]

class Wake:
    async def wake(self, session_id: str, *, cycle_id: str) -> bool:
        return True

class FailOnceAdmissionMark:
    def __init__(self, delegate):
        self.delegate = delegate
        self.failed = False
    async def mark_applied(self, *args, **kwargs):
        if not self.failed:
            self.failed = True
            raise OSError('simulated admission mark failure')
        return await self.delegate.mark_applied(*args, **kwargs)
    def __getattr__(self, name):
        return getattr(self.delegate, name)

def cycle(cycle_id):
    return ActiveAgentCycle(cycle_id=cycle_id, session_id='session', original_user_request='initial', messages_for_llm=[{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'initial'}], cycle_trace=[], original_user_message_index=1, original_input_batch_id='initial', input_runtime_generation=0)

@pytest.mark.asyncio
async def test_initial_snapshot_repairs_failed_admission_mark_without_second_r1(tmp_path):
    base = create_filesystem_input_runtime_repositories(storage_config=StorageConfigType(root_dir=str(tmp_path)))
    wrapped = FailOnceAdmissionMark(base.admissions)
    bundle = InputRuntimeRepositories(sessions=base.sessions, admissions=wrapped, inbox=base.inbox, controls=base.controls, snapshots=base.snapshots, context_revisions=base.context_revisions, emissions=base.emissions, finalizations=base.finalizations, handoffs=base.handoffs, coordination_root=base.coordination_root, coordination_locks=base.coordination_locks)
    runtime = InputAdmissionService(config=InputRuntimeConfigType(), repositories=bundle, committed_batches=Reader(Batch('initial')), wake_coordinator=Wake(), cycle_id_factory=lambda: 'cycle-a', clock=lambda: NOW, payload_size_resolver=lambda batch: batch.payload_size)
    initial = await runtime.admit_committed_batch('initial', session_id='session')
    active = cycle(initial.target_cycle_id)
    with pytest.raises(OSError):
        await runtime.checkpoint_service.run_checkpoint(checkpoint=CheckpointName.RESUME, active_cycle=active, desired_status=CycleStatus.RUNNING)
    assert await base.snapshots.get('cycle-a') is not None
    assert len(await base.context_revisions.list_for_cycle('cycle-a')) == 1
    await runtime.checkpoint_service.run_checkpoint(checkpoint=CheckpointName.RESUME, active_cycle=active, desired_status=CycleStatus.RUNNING)
    admission = await base.admissions.get_by_input_batch_id('initial')
    assert admission.state.value == 'applied'
    assert len(await base.context_revisions.list_for_cycle('cycle-a')) == 1
    assert active.messages_for_llm == [{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': 'initial'}]

@pytest.mark.asyncio
async def test_snapshot_watermark_repairs_failed_inbox_mark_without_duplicate_update(tmp_path):
    base = create_filesystem_input_runtime_repositories(storage_config=StorageConfigType(root_dir=str(tmp_path)))
    initial_batch = Batch('initial')
    addition_batch = Batch('addition')
    runtime = InputAdmissionService(config=InputRuntimeConfigType(), repositories=base, committed_batches=Reader(initial_batch, addition_batch), wake_coordinator=Wake(), cycle_id_factory=lambda: 'cycle-a', clock=lambda: NOW, payload_size_resolver=lambda batch: batch.payload_size)
    initial = await runtime.admit_committed_batch('initial', session_id='session')
    active = cycle(initial.target_cycle_id)
    await runtime.checkpoint_service.run_checkpoint(checkpoint=CheckpointName.RESUME, active_cycle=active, desired_status=CycleStatus.RUNNING)
    await runtime.admit_committed_batch('addition', session_id='session')

    snapshot = await base.snapshots.get('cycle-a')
    r1 = await base.context_revisions.get_latest('cycle-a')
    assert snapshot is not None
    assert r1 is not None
    claim = await base.inbox.claim_contiguous_range(
        'cycle-a',
        generation=0,
        after_sequence=0,
        max_items=8,
        max_bytes=1000,
        lease_seconds=300,
    )
    assert claim is not None
    applying = await base.inbox.mark_applying(claim)
    assert applying.last_cycle_sequence == 1

    r2 = CycleContextRevision(
        cycle_id='cycle-a',
        session_id='session',
        revision_number=2,
        parent_revision_ids=[r1.context_revision_id],
        reason='input_applied',
        applied_input_batch_ids=['addition'],
        applied_through_cycle_sequence=1,
        constraint_summary='checkpoint:before_llm',
        created_at=NOW,
    )
    r2 = await base.context_revisions.append_revision(r2)
    update = build_input_batch_update(
        context_revision_id=r2.context_revision_id,
        batches=[(addition_batch, 1)],
    )
    candidate = snapshot.model_copy(update={
        'messages_for_llm': [
            *snapshot.messages_for_llm,
            build_input_batch_update_message(update),
        ],
        'applied_input_batch_ids': [
            *snapshot.applied_input_batch_ids,
            'addition',
        ],
        'applied_through_cycle_sequence': 1,
        'active_context_revision_id': r2.context_revision_id,
        'snapshot_revision': snapshot.snapshot_revision + 1,
        'safe_checkpoint': CheckpointName.BEFORE_LLM,
        'updated_at': NOW,
    })
    candidate = ActiveCycleSnapshot.model_validate(candidate.model_dump(mode='python'))
    persisted = await base.snapshots.compare_and_swap(snapshot.snapshot_revision, candidate)
    assert persisted.applied_through_cycle_sequence == 1

    inbox = await base.inbox.list_for_cycle('cycle-a')
    addition = await base.admissions.get_by_input_batch_id('addition')
    assert inbox[0].state.value == 'applying'
    assert addition.state.value == 'admitted'
    assert active.active_context_revision_id == r1.context_revision_id
    assert all('input_batch_update' not in str(message.get('content')) for message in active.messages_for_llm)

    outcome = await runtime.checkpoint_service.run_checkpoint(checkpoint=CheckpointName.BEFORE_LLM, active_cycle=active, desired_status=CycleStatus.RUNNING)
    assert outcome.applied_through_cycle_sequence == 1
    inbox = await base.inbox.list_for_cycle('cycle-a')
    addition = await base.admissions.get_by_input_batch_id('addition')
    assert inbox[0].state.value == 'applied'
    assert addition.state.value == 'applied'
    assert active.active_context_revision_id == r2.context_revision_id
    updates = [
        message for message in active.messages_for_llm
        if message.get('role') == 'user'
        and isinstance(message.get('content'), str)
        and 'input_batch_update' in message['content']
    ]
    assert len(updates) == 1
    assert len(await base.context_revisions.list_for_cycle('cycle-a')) == 2

    message_count = len(active.messages_for_llm)
    retry = await runtime.checkpoint_service.run_checkpoint(checkpoint=CheckpointName.BEFORE_LLM, active_cycle=active, desired_status=CycleStatus.RUNNING)
    assert retry.applied_through_cycle_sequence == 1
    assert len(active.messages_for_llm) == message_count
    assert len(await base.context_revisions.list_for_cycle('cycle-a')) == 2
