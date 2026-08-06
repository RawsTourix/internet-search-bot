from __future__ import annotations
import asyncio
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from types import SimpleNamespace
import pytest
from src.agent.protocol import AgentAction
from src.input_runtime import CheckpointAction, CheckpointName, CheckpointOutcome, CycleStatus, InputAdmissionService, InputRuntimeConfigType, create_filesystem_input_runtime_repositories
from src.input_runtime.errors import InputRuntimeConflictError
from src.mcp.input_runtime_checkpoint_hardening import InputRuntimeCheckpointHardeningMixin
from src.mcp.input_runtime_checkpoints import InputRuntimeCheckpointMixin, _SuppressStaleCandidate, _checkpoint_active_cycle
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

    def __init__(self, *batches: Batch) -> None:
        self.batches = {batch.input_batch_id: batch for batch in batches}

    async def get_committed(self, input_batch_id: str):
        return self.batches[input_batch_id]

class Wake:

    async def wake(self, session_id: str, *, cycle_id: str) -> bool:
        return True

def active_cycle(cycle_id: str='cycle-a') -> ActiveAgentCycle:
    return ActiveAgentCycle(cycle_id=cycle_id, session_id='session', original_user_request='initial', messages_for_llm=[{'role': 'system', 'content': 'system'}, {'role': 'user', 'content': '{"type":"user_request"}'}], cycle_trace=[], original_user_message_index=1, original_input_batch_id='initial', input_runtime_generation=0)

def runtime(tmp_path, batches, *, repositories=None, max_items=8, max_bytes=1000):
    repositories = repositories or create_filesystem_input_runtime_repositories(storage_config=StorageConfigType(root_dir=str(tmp_path)))
    service = InputAdmissionService(config=InputRuntimeConfigType(max_batches_per_checkpoint=max_items, max_batch_bytes_per_checkpoint=max_bytes, max_queued_batches_per_session=32, max_queued_bytes_per_session=100000), repositories=repositories, committed_batches=Reader(*batches), wake_coordinator=Wake(), cycle_id_factory=lambda: 'cycle-a', clock=lambda: NOW, payload_size_resolver=lambda batch: batch.payload_size)
    return (service, repositories)

async def initialize(service: InputAdmissionService):
    initial = await service.admit_committed_batch('initial', session_id='session')
    active = active_cycle(initial.target_cycle_id)
    await service.checkpoint_service.run_checkpoint(checkpoint=CheckpointName.RESUME, active_cycle=active, desired_status=CycleStatus.RUNNING)
    return active

class FirstClaimBarrier:

    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.claims: list[object] = []

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    async def claim_contiguous_range(self, *args, **kwargs):
        if not self.claims:
            self.entered.set()
            await self.release.wait()
        claim = await self.delegate.claim_contiguous_range(*args, **kwargs)
        if claim is not None:
            self.claims.append(claim)
        return claim

@pytest.mark.asyncio
async def test_checkpoint_applies_only_entry_watermark_across_count_and_byte_bounds(tmp_path):
    base = create_filesystem_input_runtime_repositories(storage_config=StorageConfigType(root_dir=str(tmp_path)))
    barrier = FirstClaimBarrier(base.inbox)
    repositories = replace(base, inbox=barrier)
    service, _ = runtime(tmp_path, [Batch('initial'), Batch('one'), Batch('two'), Batch('three')], repositories=repositories, max_items=1, max_bytes=10)
    active = await initialize(service)
    await service.admit_committed_batch('one', session_id='session')
    await service.admit_committed_batch('two', session_id='session')
    task = asyncio.create_task(service.checkpoint_service.run_checkpoint(checkpoint=CheckpointName.BEFORE_LLM, active_cycle=active, desired_status=CycleStatus.RUNNING))
    await barrier.entered.wait()
    await service.admit_committed_batch('three', session_id='session')
    barrier.release.set()
    outcome = await task
    assert outcome.applied_input_batch_ids == ('one', 'two')
    assert outcome.applied_through_cycle_sequence == 2
    assert all((len(claim.items) == 1 for claim in barrier.claims))
    assert all((claim.claimed_bytes <= 10 for claim in barrier.claims))
    rows = await base.inbox.list_for_cycle('cycle-a')
    assert [(row.cycle_sequence, row.state.value) for row in rows] == [(1, 'applied'), (2, 'applied'), (3, 'queued')]
    next_outcome = await service.checkpoint_service.run_checkpoint(checkpoint=CheckpointName.BEFORE_LLM, active_cycle=active, desired_status=CycleStatus.RUNNING)
    assert next_outcome.applied_input_batch_ids == ('three',)
    assert next_outcome.applied_through_cycle_sequence == 3

class MethodBarrier:

    def __init__(self, delegate, method_name: str, predicate=None, *, after: bool=True) -> None:
        self.delegate = delegate
        self.method_name = method_name
        self.predicate = predicate or (lambda *_args, **_kwargs: True)
        self.after = after
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.used = False

    def __getattr__(self, name):
        target = getattr(self.delegate, name)
        if name != self.method_name:
            return target

        async def wrapped(*args, **kwargs):
            should_block = not self.used and self.predicate(*args, **kwargs)
            if should_block and (not self.after):
                self.used = True
                self.entered.set()
                await self.release.wait()
            result = await target(*args, **kwargs)
            if should_block and self.after:
                self.used = True
                self.entered.set()
                await self.release.wait()
            return result
        return wrapped

async def cancel_at_barrier(task: asyncio.Task, barrier: MethodBarrier):
    await barrier.entered.wait()
    task.cancel()
    barrier.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

async def assert_single_apply_after_retry(service, base, active):
    await service.checkpoint_service.run_checkpoint(checkpoint=CheckpointName.BEFORE_LLM, active_cycle=active, desired_status=CycleStatus.RUNNING)
    revisions = await base.context_revisions.list_for_cycle('cycle-a')
    assert len(revisions) == 2
    updates = [json.loads(message['content']) for message in active.messages_for_llm if message.get('role') == 'user' and isinstance(message.get('content'), str) and (json.loads(message['content']).get('type') == 'input_batch_update')]
    assert len(updates) == 1
    assert updates[0]['batches'][0]['input_batch_id'] == 'addition'

@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['mark_applying', 'revision', 'snapshot', 'mark_applied'])
async def test_apply_cancellation_reconciles_each_durable_window_without_duplicate(tmp_path, phase):
    base = create_filesystem_input_runtime_repositories(storage_config=StorageConfigType(root_dir=str(tmp_path)))
    inbox = base.inbox
    revisions = base.context_revisions
    snapshots = base.snapshots
    if phase == 'mark_applying':
        barrier = MethodBarrier(inbox, 'mark_applying')
        inbox = barrier
    elif phase == 'revision':
        barrier = MethodBarrier(revisions, 'append_revision', predicate=lambda revision: revision.revision_number > 1)
        revisions = barrier
    elif phase == 'snapshot':
        barrier = MethodBarrier(snapshots, 'compare_and_swap', predicate=lambda _expected, snapshot: snapshot.applied_through_cycle_sequence > 0)
        snapshots = barrier
    else:
        barrier = MethodBarrier(inbox, 'mark_applied', after=False)
        inbox = barrier
    repositories = replace(base, inbox=inbox, context_revisions=revisions, snapshots=snapshots)
    service, _ = runtime(tmp_path, [Batch('initial'), Batch('addition')], repositories=repositories)
    active = await initialize(service)
    await service.admit_committed_batch('addition', session_id='session')
    task = asyncio.create_task(service.checkpoint_service.run_checkpoint(checkpoint=CheckpointName.BEFORE_LLM, active_cycle=active, desired_status=CycleStatus.RUNNING))
    await cancel_at_barrier(task, barrier)
    snapshot = await base.snapshots.get('cycle-a')
    rows = await base.inbox.list_for_cycle('cycle-a')
    if phase in {'mark_applying', 'revision'}:
        assert snapshot.applied_through_cycle_sequence == 0
        assert rows[0].state.value == 'queued'
    else:
        assert snapshot.applied_through_cycle_sequence == 1
        assert rows[0].state.value == 'applied'
    await assert_single_apply_after_retry(service, base, active)

class RepeatedCancellationInbox:

    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.apply_entered = asyncio.Event()
        self.apply_release = asyncio.Event()
        self.cleanup_entered = asyncio.Event()
        self.cleanup_release = asyncio.Event()

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    async def mark_applying(self, claim):
        result = await self.delegate.mark_applying(claim)
        self.apply_entered.set()
        await self.apply_release.wait()
        return result

    async def requeue_claim(self, claim, *, error_code=None):
        self.cleanup_entered.set()
        await self.cleanup_release.wait()
        return await self.delegate.requeue_claim(claim, error_code=error_code)

@pytest.mark.asyncio
async def test_repeated_cancellation_does_not_interrupt_claim_cleanup(tmp_path):
    base = create_filesystem_input_runtime_repositories(storage_config=StorageConfigType(root_dir=str(tmp_path)))
    wrapped = RepeatedCancellationInbox(base.inbox)
    service, _ = runtime(tmp_path, [Batch('initial'), Batch('addition')], repositories=replace(base, inbox=wrapped))
    active = await initialize(service)
    await service.admit_committed_batch('addition', session_id='session')
    task = asyncio.create_task(service.checkpoint_service.run_checkpoint(checkpoint=CheckpointName.BEFORE_LLM, active_cycle=active, desired_status=CycleStatus.RUNNING))
    await wrapped.apply_entered.wait()
    task.cancel()
    wrapped.apply_release.set()
    await wrapped.cleanup_entered.wait()
    task.cancel()
    wrapped.cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    rows = await base.inbox.list_for_cycle('cycle-a')
    assert rows[0].state.value == 'queued'
    await assert_single_apply_after_retry(service, base, active)

class SessionRepository:

    def __init__(self, state):
        self.state = state

    async def get(self, _session_id):
        return self.state

class CheckpointService:

    def __init__(self, *, input_applied=False) -> None:
        self.calls = []
        self.input_applied = input_applied

    async def run_checkpoint(self, **kwargs):
        self.calls.append(kwargs)
        action = CheckpointAction.INPUT_APPLIED if self.input_applied and (not kwargs.get('terminal_sync')) else CheckpointAction.CONTINUE
        return CheckpointOutcome(checkpoint=kwargs['checkpoint'], action=action, context_revision_id='ctxrev_' + '1' * 32, applied_through_cycle_sequence=1 if action == CheckpointAction.INPUT_APPLIED else 0, applied_input_batch_ids=('addition',) if action == CheckpointAction.INPUT_APPLIED else ())

class LoopBase:

    def __init__(self) -> None:
        self.llm_calls = 0
        self.event_calls = 0
        self.raise_event = False

    async def _call_main_llm_with_context_recovery(self, **kwargs):
        self.llm_calls += 1
        return ({'content': 'ok', 'tool_calls': []}, kwargs['active_cycle'].messages_for_llm)

    async def _emit_progress_event(self, *args, **kwargs):
        self.event_calls += 1
        if self.raise_event:
            raise RuntimeError('terminal compatibility failed')
        return 'emitted'

class LoopHarness(InputRuntimeCheckpointHardeningMixin, InputRuntimeCheckpointMixin, LoopBase):

    def __init__(self, binding) -> None:
        self.binding = binding
        super().__init__()

    def _checkpoint_service(self):
        return self.binding.checkpoint_service

@pytest.fixture
def bind_runtime(monkeypatch):

    def install(state, service):
        binding = SimpleNamespace(config=SimpleNamespace(enabled=True), repositories=SimpleNamespace(sessions=SessionRepository(state)), checkpoint_service=service)
        monkeypatch.setattr('src.mcp.input_runtime_checkpoint_hardening.get_input_runtime_binding', lambda: binding)
        return binding
    return install

@pytest.mark.asyncio
@pytest.mark.parametrize(('state', 'cycle_id', 'generation', 'reason'), [(None, 'cycle-a', 0, 'checkpoint_session_state_missing'), (SimpleNamespace(active_cycle_id='cycle-new', generation=0), 'cycle-old', 0, 'checkpoint_active_cycle_mismatch'), (SimpleNamespace(active_cycle_id='cycle-a', generation=2), 'cycle-a', 1, 'checkpoint_runner_generation_stale')])
async def test_stale_checkpoint_authority_never_reaches_next_llm(bind_runtime, state, cycle_id, generation, reason):
    service = CheckpointService()
    harness = LoopHarness(bind_runtime(state, service))
    active = active_cycle(cycle_id)
    active.input_runtime_generation = generation
    with pytest.raises(RuntimeError, match=reason):
        await harness._call_main_llm_with_context_recovery(active_cycle=active, state=SimpleNamespace(), session_id='session', progress_callback=None, tools=[], context='', include_iteration_runtime=False)
    assert harness.llm_calls == 0
    assert active.input_runtime_generation == generation

@pytest.mark.asyncio
async def test_matching_checkpoint_authority_reaches_llm(bind_runtime):
    state = SimpleNamespace(active_cycle_id='cycle-a', generation=0)
    harness = LoopHarness(bind_runtime(state, CheckpointService()))
    active = active_cycle()
    await harness._call_main_llm_with_context_recovery(active_cycle=active, state=SimpleNamespace(), session_id='session', progress_callback=None, tools=[], context='', include_iteration_runtime=False)
    assert harness.llm_calls == 1

@pytest.mark.asyncio
async def test_preterminal_failure_does_not_sync_terminal_snapshot(bind_runtime):
    service = CheckpointService()
    harness = LoopHarness(bind_runtime(SimpleNamespace(active_cycle_id='cycle-a', generation=0), service))
    harness.raise_event = True
    active = active_cycle()
    token = _checkpoint_active_cycle.set(active)
    try:
        with pytest.raises(RuntimeError, match='terminal compatibility failed'):
            await harness._emit_progress_event(event_type='cycle_done')
    finally:
        _checkpoint_active_cycle.reset(token)
    assert [call['desired_status'] for call in service.calls] == [CycleStatus.RUNNING]
    assert all((not call.get('terminal_sync') for call in service.calls))

@pytest.mark.asyncio
async def test_successful_terminal_event_only_runs_preterminal_guard(bind_runtime):
    service = CheckpointService()
    harness = LoopHarness(bind_runtime(SimpleNamespace(active_cycle_id='cycle-a', generation=0), service))
    active = active_cycle()
    token = _checkpoint_active_cycle.set(active)
    try:
        assert await harness._emit_progress_event(event_type='cycle_done') == 'emitted'
    finally:
        _checkpoint_active_cycle.reset(token)
    assert [call['desired_status'] for call in service.calls] == [CycleStatus.RUNNING]
    assert all((not call.get('terminal_sync') for call in service.calls))

@pytest.mark.asyncio
async def test_preterminal_pending_input_suppresses_candidate(bind_runtime):
    service = CheckpointService(input_applied=True)
    harness = LoopHarness(bind_runtime(SimpleNamespace(active_cycle_id='cycle-a', generation=0), service))
    active = active_cycle()
    active.messages_for_llm.append({'role': 'assistant', 'content': AgentAction(status='done', action='answer', final_answer='stale').model_dump_json()})
    token = _checkpoint_active_cycle.set(active)
    try:
        with pytest.raises(_SuppressStaleCandidate):
            await harness._emit_progress_event(event_type='cycle_done')
    finally:
        _checkpoint_active_cycle.reset(token)
    assert harness.event_calls == 0
    assert all((call['desired_status'] == CycleStatus.RUNNING for call in service.calls))

@pytest.mark.asyncio
async def test_input_after_last_preterminal_check_remains_deferred_to_ir7(bind_runtime):
    service = CheckpointService()
    harness = LoopHarness(bind_runtime(SimpleNamespace(active_cycle_id='cycle-a', generation=0), service))
    active = active_cycle()
    token = _checkpoint_active_cycle.set(active)
    try:
        await harness._emit_progress_event(event_type='cycle_done')
    finally:
        _checkpoint_active_cycle.reset(token)
    assert len(service.calls) == 1
    assert service.calls[0]['apply_input'] is True
    assert service.calls[0]['desired_status'] == CycleStatus.RUNNING

class FailSnapshotCAS:

    def __init__(self, delegate) -> None:
        self.delegate = delegate

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    async def compare_and_swap(self, expected_revision, snapshot):
        if snapshot.applied_through_cycle_sequence > 0:
            raise InputRuntimeConflictError('stale snapshot revision')
        return await self.delegate.compare_and_swap(expected_revision, snapshot)

@pytest.mark.asyncio
async def test_managed_stale_cas_returns_typed_interruption_and_requeues(tmp_path):
    base = create_filesystem_input_runtime_repositories(storage_config=StorageConfigType(root_dir=str(tmp_path)))
    service, _ = runtime(tmp_path, [Batch('initial'), Batch('addition')], repositories=replace(base, snapshots=FailSnapshotCAS(base.snapshots)))
    active = await initialize(service)
    await service.admit_committed_batch('addition', session_id='session')
    outcome = await service.checkpoint_service.run_checkpoint(checkpoint=CheckpointName.BEFORE_LLM, active_cycle=active, desired_status=CycleStatus.RUNNING)
    assert outcome.action == CheckpointAction.INTERRUPT
    assert outcome.reason_code == 'checkpoint_snapshot_cas_exhausted'
    rows = await base.inbox.list_for_cycle('cycle-a')
    assert rows[0].state.value == 'queued'

class RealTerminalBase:

    def __init__(self) -> None:
        self.raise_event = False
        self.event_calls = 0
        self.late_admitter = None

    async def _emit_progress_event(self, *args, **kwargs):
        self.event_calls += 1
        if self.raise_event:
            raise RuntimeError('terminal compatibility failed')
        if self.late_admitter is not None:
            await self.late_admitter()
        return 'emitted'

class RealTerminalHarness(InputRuntimeCheckpointHardeningMixin, InputRuntimeCheckpointMixin, RealTerminalBase):
    pass

@pytest.mark.asyncio
async def test_real_preterminal_failure_leaves_snapshot_running(tmp_path):
    service, repositories = runtime(tmp_path, [Batch('initial')])
    active = await initialize(service)
    harness = RealTerminalHarness()
    harness.raise_event = True
    token = _checkpoint_active_cycle.set(active)
    try:
        with pytest.raises(RuntimeError, match='terminal compatibility failed'):
            await harness._emit_progress_event(event_type='cycle_done')
    finally:
        _checkpoint_active_cycle.reset(token)
    snapshot = await repositories.snapshots.get('cycle-a')
    assert snapshot.status == CycleStatus.RUNNING

@pytest.mark.asyncio
async def test_terminal_output_failure_after_status_leaves_snapshot_running(tmp_path):
    service, repositories = runtime(tmp_path, [Batch('initial')])
    active = await initialize(service)
    harness = RealTerminalHarness()
    token = _checkpoint_active_cycle.set(active)
    try:
        await harness._emit_progress_event(event_type='cycle_done')
    finally:
        _checkpoint_active_cycle.reset(token)
    await service.record_cycle_status(session_id='session', cycle_id='cycle-a', status=CycleStatus.DONE)
    snapshot = await repositories.snapshots.get('cycle-a')
    assert snapshot.status == CycleStatus.RUNNING

@pytest.mark.asyncio
async def test_real_successful_terminal_path_syncs_snapshot_done(tmp_path):
    service, repositories = runtime(tmp_path, [Batch('initial')])
    active = await initialize(service)
    admission = await repositories.admissions.get_by_input_batch_id('initial')
    assert await service.begin_runtime_handoff(admission, handoff_token='terminal-token')
    harness = RealTerminalHarness()
    token = _checkpoint_active_cycle.set(active)
    try:
        await harness._emit_progress_event(event_type='cycle_done')
    finally:
        _checkpoint_active_cycle.reset(token)
    before = await repositories.snapshots.get('cycle-a')
    assert before.status == CycleStatus.RUNNING
    await service.record_cycle_status(session_id='session', cycle_id='cycle-a', status=CycleStatus.DONE)
    await service.complete_runtime_handoff(admission, handoff_token='terminal-token')
    snapshot = await repositories.snapshots.get('cycle-a')
    assert snapshot.status == CycleStatus.DONE

@pytest.mark.asyncio
async def test_real_preterminal_input_suppresses_done_candidate(tmp_path):
    service, repositories = runtime(tmp_path, [Batch('initial'), Batch('addition')])
    active = await initialize(service)
    await service.admit_committed_batch('addition', session_id='session')
    active.messages_for_llm.append({'role': 'assistant', 'content': AgentAction(status='done', action='answer', final_answer='stale').model_dump_json()})
    harness = RealTerminalHarness()
    token = _checkpoint_active_cycle.set(active)
    try:
        with pytest.raises(_SuppressStaleCandidate):
            await harness._emit_progress_event(event_type='cycle_done')
    finally:
        _checkpoint_active_cycle.reset(token)
    snapshot = await repositories.snapshots.get('cycle-a')
    assert snapshot.status == CycleStatus.RUNNING
    assert snapshot.applied_through_cycle_sequence == 1
    assert harness.event_calls == 0

@pytest.mark.asyncio
async def test_real_input_after_last_guard_stays_queued_for_ir7(tmp_path):
    service, repositories = runtime(tmp_path, [Batch('initial'), Batch('late')])
    active = await initialize(service)
    admission = await repositories.admissions.get_by_input_batch_id('initial')
    assert await service.begin_runtime_handoff(admission, handoff_token='late-token')
    harness = RealTerminalHarness()

    async def admit_late():
        await service.admit_committed_batch('late', session_id='session')
    harness.late_admitter = admit_late
    token = _checkpoint_active_cycle.set(active)
    try:
        await harness._emit_progress_event(event_type='cycle_done')
    finally:
        _checkpoint_active_cycle.reset(token)
    state = await service.record_cycle_status(session_id='session', cycle_id='cycle-a', status=CycleStatus.DONE)
    assert state.cycle_status == CycleStatus.INTERRUPTED
    await service.complete_runtime_handoff(admission, handoff_token='late-token')
    snapshot = await repositories.snapshots.get('cycle-a')
    rows = await repositories.inbox.list_for_cycle('cycle-a')
    assert snapshot.status == CycleStatus.RUNNING
    assert snapshot.applied_through_cycle_sequence == 0
    assert [(row.cycle_sequence, row.state.value) for row in rows] == [(1, 'queued')]

class WrongCommittedReader(Reader):

    async def get_committed(self, input_batch_id: str):
        batch = await super().get_committed(input_batch_id)
        if input_batch_id == 'addition':
            return Batch('wrong', session_id=batch.session_id)
        return batch

@pytest.mark.asyncio
async def test_invalid_committed_relation_returns_managed_interruption(tmp_path):
    repositories = create_filesystem_input_runtime_repositories(storage_config=StorageConfigType(root_dir=str(tmp_path)))
    service = InputAdmissionService(config=InputRuntimeConfigType(), repositories=repositories, committed_batches=WrongCommittedReader(Batch('initial'), Batch('addition')), wake_coordinator=Wake(), cycle_id_factory=lambda: 'cycle-a', clock=lambda: NOW, payload_size_resolver=lambda batch: batch.payload_size)
    active = await initialize(service)
    await service.admit_committed_batch('addition', session_id='session')
    outcome = await service.checkpoint_service.run_checkpoint(checkpoint=CheckpointName.BEFORE_LLM, active_cycle=active, desired_status=CycleStatus.RUNNING)
    assert outcome.action == CheckpointAction.INTERRUPT
    assert outcome.reason_code == 'invalid_committed_relation'
    rows = await repositories.inbox.list_for_cycle('cycle-a')
    assert rows[0].state.value == 'queued'

@pytest.mark.asyncio
async def test_invalid_protocol_sequence_returns_managed_interruption(tmp_path):
    service, _ = runtime(tmp_path, [Batch('initial')])
    active = await initialize(service)
    active.messages_for_llm.extend([{'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'call-a', 'type': 'function', 'function': {'name': 'demo', 'arguments': '{}'}}]}])
    outcome = await service.checkpoint_service.run_checkpoint(checkpoint=CheckpointName.AFTER_TOOL_BLOCK, active_cycle=active, desired_status=CycleStatus.RUNNING)
    assert outcome.action == CheckpointAction.INTERRUPT
    assert outcome.reason_code == 'invalid_active_message_sequence'

class DivergentLatestRevision:

    def __init__(self, delegate) -> None:
        self.delegate = delegate
        self.diverge = False

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    async def get_latest(self, cycle_id: str):
        latest = await self.delegate.get_latest(cycle_id)
        if not self.diverge or latest is None:
            return latest
        return SimpleNamespace(context_revision_id='ctxrev_' + '9' * 32, parent_revision_ids=[], reason='unrelated', applied_input_batch_ids=[], applied_through_cycle_sequence=latest.applied_through_cycle_sequence, revision_number=latest.revision_number + 1)

@pytest.mark.asyncio
async def test_divergent_revision_returns_managed_interruption(tmp_path):
    base = create_filesystem_input_runtime_repositories(storage_config=StorageConfigType(root_dir=str(tmp_path)))
    divergent = DivergentLatestRevision(base.context_revisions)
    service, _ = runtime(tmp_path, [Batch('initial'), Batch('addition')], repositories=replace(base, context_revisions=divergent))
    active = await initialize(service)
    divergent.diverge = True
    await service.admit_committed_batch('addition', session_id='session')
    outcome = await service.checkpoint_service.run_checkpoint(checkpoint=CheckpointName.BEFORE_LLM, active_cycle=active, desired_status=CycleStatus.RUNNING)
    assert outcome.action == CheckpointAction.INTERRUPT
    assert outcome.reason_code == 'divergent_context_revision'
    rows = await base.inbox.list_for_cycle('cycle-a')
    assert rows[0].state.value == 'queued'
