from __future__ import annotations
import asyncio
import json
from types import SimpleNamespace
import pytest
from src.agent.protocol import AgentAction
from src.input_runtime import CheckpointAction, CheckpointName, CheckpointOutcome
from src.mcp.input_runtime_checkpoints import InputRuntimeCheckpointMixin, _SuppressStaleCandidate
from src.mcp.manager_runtime_context import reset_manager_context, set_manager_context
from src.mcp.waiting_user_batch_continuation import WaitingUserBatchContinuationMixin
from src.runtime import ActiveAgentCycle

def cycle(status='running'):
    return ActiveAgentCycle(cycle_id='cycle-a', session_id='session', original_user_request='initial', messages_for_llm=[{'role':'system','content':'system'},{'role':'user','content':'initial'}], cycle_trace=[], original_user_message_index=1, status=status)

class WaitingBase:
    def __init__(self):
        self.pending, self.received = cycle('waiting_user'), None
    def _get_or_create_session(self, _session_id):
        return SimpleNamespace(pending_cycle=self.pending)
    async def process_query(self, *args, **kwargs):
        self.received = (args, kwargs)
        return 'ok'
class WaitingHarness(WaitingUserBatchContinuationMixin, WaitingBase):
    pass

@pytest.mark.asyncio
async def test_waiting_compatibility_does_not_forward_reply_batch_or_text():
    harness = WaitingHarness()
    assert await harness.process_query('legacy-reply', session_id='session', input_batch=SimpleNamespace(input_batch_id='reply')) == 'ok'
    args, kwargs = harness.received
    assert args[0] == ''
    assert 'input_batch' not in kwargs

class LLMBase:
    def __init__(self):
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.pending_visible = False
    async def _call_main_llm_with_context_recovery(self, **kwargs):
        self.entered.set()
        await self.release.wait()
        return {'content':AgentAction(status='done', action='answer', final_answer='stale').model_dump_json(),'tool_calls':[]}, kwargs['active_cycle'].messages_for_llm
class LLMHarness(InputRuntimeCheckpointMixin, LLMBase):
    def _checkpoint_service(self):
        return object()
    async def _run_input_checkpoint(self, checkpoint, **kwargs):
        if checkpoint == CheckpointName.BEFORE_FINAL_PROCESSING and self.pending_visible:
            return CheckpointOutcome(checkpoint=checkpoint, action=CheckpointAction.INPUT_APPLIED, context_revision_id='ctxrev_'+'1'*32, applied_through_cycle_sequence=1, applied_input_batch_ids=('addition',))
        return CheckpointOutcome(checkpoint=checkpoint, action=CheckpointAction.CONTINUE, context_revision_id='ctxrev_'+'0'*32)

@pytest.mark.asyncio
async def test_input_arriving_during_llm_suppresses_stale_final_after_response():
    harness, active = LLMHarness(), cycle()
    task = asyncio.create_task(harness._call_main_llm_with_context_recovery(active_cycle=active, state=SimpleNamespace(), session_id='session', progress_callback=None, tools=[], context='', include_iteration_runtime=False))
    await harness.entered.wait()
    harness.pending_visible = True
    harness.release.set()
    response, _ = await task
    action = AgentAction.model_validate_json(response['content'])
    assert (action.status, action.action) == ('running','continue')

class RestartBase:
    def __init__(self):
        self.calls, self.session, self.active = 0, SimpleNamespace(pending_cycle=None), cycle()
    def _checkpoint_service(self):
        return None
    def _get_or_create_session(self, _session_id):
        return self.session
    def _activate_manager_context(self, **kwargs):
        return SimpleNamespace(active_cycle=kwargs['active_cycle'])
    async def process_query(self, *args, **kwargs):
        self.calls += 1
        self._activate_manager_context(active_cycle=self.active, state=SimpleNamespace(), session_id='session', progress_callback=None)
        token = set_manager_context(SimpleNamespace(active_cycle=self.active))
        try:
            if self.calls == 1:
                raise _SuppressStaleCandidate()
            return SimpleNamespace(status='done', content='done', can_resume=False)
        finally:
            reset_manager_context(token)
class RestartHarness(InputRuntimeCheckpointMixin, RestartBase):
    pass

@pytest.mark.asyncio
async def test_checkpoint_restart_keeps_cycle_after_manager_context_reset():
    harness = RestartHarness()
    result = await harness.process_query('initial', session_id='session')
    assert result.content == 'done'
    assert harness.calls == 2
    assert harness.session.pending_cycle is harness.active

def test_tool_block_checkpoint_waits_for_all_matching_results():
    messages=[{'role':'assistant','content':None,'tool_calls':[{'id':'one','type':'function','function':{}},{'id':'two','type':'function','function':{}}]},{'role':'tool','tool_call_id':'one','content':'result'}]
    assert not InputRuntimeCheckpointMixin._last_block_is_complete_tool_block(messages)
    messages.append({'role':'tool','tool_call_id':'two','content':'result'})
    assert InputRuntimeCheckpointMixin._last_block_is_complete_tool_block(messages)

def test_empty_compatibility_resume_message_is_removed_before_checkpoint():
    active=cycle()
    active.messages_for_llm.append({'role':'user','content':json.dumps({'type':'user_reply_during_waiting_user','reply':'','previous_question':'question'})})
    active.cycle_trace.append({'type':'user_reply_during_waiting_user','reply':''})
    InputRuntimeCheckpointMixin._drop_internal_resume_message(active)
    assert active.messages_for_llm[-1]['content']=='initial'
    assert active.cycle_trace==[]
