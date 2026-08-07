from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from src.input_runtime import CheckpointName, CycleStatus, InputAdmissionService, InputRuntimeConfigType, InputRuntimeRepositories, create_filesystem_input_runtime_repositories
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

class FailOnceInboxMark:
    def __init__(self, delegate):
        self.delegate = delegate
        self.failed = False
    async def mark_applied(self, *args, **kwargs):
        if not self.failed:
            self.failed = True
            raise OSError('simulated inbox mark failure')
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
    wrapped_inbox = FailOnceInboxMark(base.inbox)
    bundle = InputRuntimeRepositories(sessions=base.sessions, admissions=base.admissions, inbox=wrapped_inbox, controls=base.controls, snapshots=base.snapshots, context_revisions=base.context_revisions, emissions=base.emissions, finalizations=base.finalizations, handoffs=base.handoffs, coordination_root=base.coordination_root, coordination_locks=base.coordination_locks)
    runtime = InputAdmissionService(config=InputRuntimeConfigType(), repositories=bundle, committed_batches=Reader(Batch('initial'), Batch('addition')), wake_coordinator=Wake(), cycle_id_factory=lambda: 'cycle-a', clock=lambda: NOW, payload_size_resolver=lambda batch: batch.payload_size)
    initial = await runtime.admit_committed_batch('initial', session_id='session')
    active = cycle(initial.target_cycle_id)
    await runtime.checkpoint_service.run_checkpoint(checkpoint=CheckpointName.RESUME, active_cycle=active, desired_status=CycleStatus.RUNNING)
    await runtime.admit_committed_batch('addition', session_id='session')
    await runtime.checkpoint_service.run_checkpoint(checkpoint=CheckpointName.BEFORE_LLM, active_cycle=active, desired_status=CycleStatus.RUNNING)

    snapshot = await base.snapshots.get('cycle-a')
    assert snapshot.applied_through_cycle_sequence == 1
    inbox = await base.inbox.list_for_cycle('cycle-a')
    assert inbox[0].state.value == 'applied'
    addition = await base.admissions.get_by_input_batch_id('addition')
    assert addition.state.value == 'applied'
    revisions = await base.context_revisions.list_for_cycle('cycle-a')
    assert [item.revision_number for item in revisions] == [1, 2]
    updates = [
        message for message in active.messages_for_llm
        if message.get('role') == 'user'
        and isinstance(message.get('content'), str)
        and 'input_batch_update' in message['content']
    ]
    assert len(updates) == 1

    message_count = len(active.messages_for_llm)
    await runtime.checkpoint_service.run_checkpoint(checkpoint=CheckpointName.BEFORE_LLM, active_cycle=active, desired_status=CycleStatus.RUNNING)
    assert len(active.messages_for_llm) == message_count
    assert len(await base.context_revisions.list_for_cycle('cycle-a')) == 2
    inbox = await base.inbox.list_for_cycle('cycle-a')
    assert inbox[0].state.value == 'applied'
    addition = await base.admissions.get_by_input_batch_id('addition')
    assert addition.state.value == 'applied'
