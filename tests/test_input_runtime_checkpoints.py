from __future__ import annotations
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from src.input_runtime import CheckpointAction, CheckpointName, CycleStatus, InputAdmissionService, InputRuntimeConfigType, create_filesystem_input_runtime_repositories
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
    def __init__(self, *batches: Batch):
        self.batches = {item.input_batch_id: item for item in batches}
    async def get_committed(self, input_batch_id: str):
        return self.batches[input_batch_id]

class Wake:
    async def wake(self, session_id: str, *, cycle_id: str) -> bool:
        return True

def active_cycle(cycle_id: str) -> ActiveAgentCycle:
    return ActiveAgentCycle(cycle_id=cycle_id, session_id='session', original_user_request='initial', messages_for_llm=[{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': '{"type":"user_request"}'}], cycle_trace=[], original_user_message_index=1, active_plan_id='plan-a', active_plan_revision=2, active_plan_node_id='node-a')

def build_runtime(tmp_path, batches, *, max_per_checkpoint=1):
    repositories = create_filesystem_input_runtime_repositories(storage_config=StorageConfigType(root_dir=str(tmp_path)))
    config = InputRuntimeConfigType(max_queued_batches_per_session=16, max_queued_bytes_per_session=100000, max_batches_per_checkpoint=max_per_checkpoint, max_batch_bytes_per_checkpoint=100000)
    service = InputAdmissionService(config=config, repositories=repositories, committed_batches=Reader(*batches), wake_coordinator=Wake(), cycle_id_factory=lambda: 'cycle-a', clock=lambda: NOW, payload_size_resolver=lambda batch: batch.payload_size)
    return service, repositories

@pytest.mark.asyncio
async def test_cp_resume_drains_bounded_ranges_through_entry_watermark(tmp_path):
    batches = [Batch('initial'), Batch('one'), Batch('two'), Batch('reply')]
    service, repositories = build_runtime(tmp_path, batches, max_per_checkpoint=1)
    initial = await service.admit_committed_batch('initial', session_id='session')
    active = active_cycle(initial.target_cycle_id)
    active.original_input_batch_id = 'initial'
    active.input_runtime_generation = 0
    await service.checkpoint_service.run_checkpoint(checkpoint=CheckpointName.RESUME, active_cycle=active, desired_status=CycleStatus.RUNNING)
    await service.admit_committed_batch('one', session_id='session')
    await service.admit_committed_batch('two', session_id='session')
    state = await repositories.sessions.get('session')
    waiting = state.model_copy(update={'cycle_status': CycleStatus.WAITING_USER, 'revision': state.revision + 1, 'updated_at': NOW})
    await repositories.sessions.compare_and_swap(state.revision, waiting)
    await service.admit_committed_batch('reply', session_id='session')
    outcome = await service.checkpoint_service.run_checkpoint(checkpoint=CheckpointName.RESUME, active_cycle=active, desired_status=CycleStatus.RUNNING)
    assert outcome.action == CheckpointAction.INPUT_APPLIED
    assert outcome.applied_input_batch_ids == ('one', 'two', 'reply')
    assert outcome.applied_through_cycle_sequence == 3
    updates = [json.loads(message['content']) for message in active.messages_for_llm if message.get('role') == 'user' and json.loads(message['content']).get('type') == 'input_batch_update']
    assert [update['batches'][0]['input_batch_id'] for update in updates] == ['one', 'two', 'reply']
    assert [update['batches'][0]['cycle_sequence'] for update in updates] == [1, 2, 3]
    revisions = await repositories.context_revisions.list_for_cycle('cycle-a')
    assert [item.revision_number for item in revisions] == [1, 2, 3, 4]
    assert [item.parent_revision_ids for item in revisions[1:]] == [[revisions[0].context_revision_id], [revisions[1].context_revision_id], [revisions[2].context_revision_id]]

@pytest.mark.asyncio
async def test_noop_checkpoint_updates_snapshot_without_semantic_revision(tmp_path):
    service, repositories = build_runtime(tmp_path, [Batch('initial')])
    initial = await service.admit_committed_batch('initial', session_id='session')
    active = active_cycle(initial.target_cycle_id)
    active.original_input_batch_id = 'initial'
    active.input_runtime_generation = 0
    await service.checkpoint_service.run_checkpoint(checkpoint=CheckpointName.RESUME, active_cycle=active, desired_status=CycleStatus.RUNNING)
    before = await repositories.context_revisions.list_for_cycle('cycle-a')
    outcome = await service.checkpoint_service.run_checkpoint(checkpoint=CheckpointName.BEFORE_WAITING, active_cycle=active, desired_status=CycleStatus.WAITING_USER, waiting_question='Need clarification?')
    assert outcome.action == CheckpointAction.CONTINUE
    snapshot = await repositories.snapshots.get('cycle-a')
    assert snapshot.status == CycleStatus.WAITING_USER
    assert snapshot.waiting_question == 'Need clarification?'
    assert snapshot.safe_checkpoint == CheckpointName.BEFORE_WAITING
    assert snapshot.active_plan_id == 'plan-a'
    assert snapshot.active_plan_revision == 2
    assert snapshot.active_plan_node_id == 'node-a'
    assert await repositories.context_revisions.list_for_cycle('cycle-a') == before
