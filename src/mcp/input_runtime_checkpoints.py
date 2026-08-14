"""Safe input-runtime checkpoint and IR-7 finalization hooks."""
from __future__ import annotations
import json
from contextvars import ContextVar
from typing import Any

from ..agent.protocol import AgentAction
from ..input_runtime import CheckpointAction, CheckpointName, CycleStatus, get_input_runtime_binding
from ..input_runtime.errors import InputRuntimeConflictError
from ..input_runtime.models import FinalizationState
from ..interaction.ir7_output_barrier import abandon_uncommitted_final_output
from ..runtime.finalization_bridge import get_final_output_assembler

_checkpoint_restart: ContextVar[bool] = ContextVar('input_runtime_checkpoint_restart', default=False)
_checkpoint_active_cycle: ContextVar[Any | None] = ContextVar('input_runtime_checkpoint_active_cycle', default=None)
_checkpoint_finalization_candidate: ContextVar[Any | None] = ContextVar('input_runtime_finalization_candidate', default=None)
_checkpoint_finalization_id: ContextVar[str | None] = ContextVar('input_runtime_finalization_id', default=None)


class _SuppressStaleCandidate(BaseException):
    """Unwind one stale candidate without entering generic error handling."""


class InputRuntimeCheckpointMixin:
    """Delegate safe checkpoint/finalization work without storage logic in MCPClient."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._input_runtime_checkpoint_service = None
        self._input_runtime_seen_cycles: set[str] = set()
        super().__init__(*args, **kwargs)

    def _checkpoint_service(self):
        binding = get_input_runtime_binding()
        if binding is None or not binding.config.enabled:
            return None
        if self._input_runtime_checkpoint_service is not binding.checkpoint_service:
            self._input_runtime_checkpoint_service = binding.checkpoint_service
        return self._input_runtime_checkpoint_service

    def _activate_manager_context(self, *, active_cycle, state, session_id: str, progress_callback):
        context = super()._activate_manager_context(active_cycle=active_cycle, state=state, session_id=session_id, progress_callback=progress_callback)
        _checkpoint_active_cycle.set(active_cycle)
        return context

    @staticmethod
    def _last_block_is_complete_tool_block(messages: list[dict[str, Any]]) -> bool:
        if not messages or messages[-1].get('role') != 'tool':
            return False
        index = len(messages) - 1
        result_ids: list[str] = []
        while index >= 0 and messages[index].get('role') == 'tool':
            result_ids.append(str(messages[index].get('tool_call_id') or ''))
            index -= 1
        if index < 0:
            return False
        assistant = messages[index]
        calls = assistant.get('tool_calls')
        if assistant.get('role') != 'assistant' or not isinstance(calls, list):
            return False
        call_ids = [str(item.get('id') or '') for item in calls]
        return bool(call_ids) and len(call_ids) == len(set(call_ids)) and len(result_ids) == len(set(result_ids)) and set(call_ids) == set(result_ids)

    @staticmethod
    def _drop_internal_resume_message(active_cycle: Any) -> None:
        if not active_cycle.messages_for_llm:
            return
        message = active_cycle.messages_for_llm[-1]
        if message.get('role') != 'user':
            return
        try:
            payload = json.loads(message.get('content') or '')
        except Exception:
            return
        if not isinstance(payload, dict) or payload.get('type') not in {'user_reply_during_waiting_user', 'user_resume_interrupted_cycle'} or str(payload.get('reply') or '').strip():
            return
        active_cycle.messages_for_llm.pop()
        if active_cycle.cycle_trace:
            last = active_cycle.cycle_trace[-1]
            if last.get('type') == payload.get('type') and not str(last.get('reply') or '').strip():
                active_cycle.cycle_trace.pop()

    @staticmethod
    def _agent_action(response: dict[str, Any]) -> AgentAction | None:
        if response.get('tool_calls'):
            return None
        content = response.get('content')
        if not isinstance(content, str) or not content.strip():
            return None
        try:
            return AgentAction.model_validate_json(content)
        except Exception:
            return None

    @staticmethod
    def _continue_response(response: dict[str, Any]) -> dict[str, Any]:
        updated = dict(response)
        updated['content'] = AgentAction(status='running', action='continue').model_dump_json()
        updated['tool_calls'] = []
        return updated

    @staticmethod
    def _last_candidate(active_cycle: Any) -> AgentAction | None:
        if not active_cycle.messages_for_llm:
            return None
        message = active_cycle.messages_for_llm[-1]
        if message.get('role') != 'assistant' or message.get('tool_calls'):
            return None
        content = message.get('content')
        if not isinstance(content, str):
            return None
        try:
            return AgentAction.model_validate_json(content)
        except Exception:
            return None

    @classmethod
    def _remove_stale_candidate(cls, active_cycle: Any) -> None:
        candidate = cls._last_candidate(active_cycle)
        if candidate is not None and candidate.status in {'waiting_user', 'done', 'error'}:
            active_cycle.messages_for_llm.pop()

    def _trace_suppression(self, active_cycle: Any, *, checkpoint: CheckpointName) -> None:
        tracer = getattr(self, '_trace_event', None)
        if tracer is not None:
            tracer(active_cycle.cycle_trace, 'input_runtime_candidate_suppressed', checkpoint=checkpoint.value, context_revision_id=getattr(active_cycle, 'active_context_revision_id', None), applied_through_cycle_sequence=getattr(active_cycle, 'applied_through_cycle_sequence', 0))

    async def _run_input_checkpoint(self, checkpoint: CheckpointName, *, active_cycle: Any | None=None, desired_status: CycleStatus | None=None, waiting_question: str | None=None, interruption_reason: str | None=None, apply_input: bool=True):
        service = self._checkpoint_service()
        active_cycle = active_cycle or _checkpoint_active_cycle.get()
        if service is None or active_cycle is None:
            return None
        binding = get_input_runtime_binding()
        if binding is None:
            return None
        state = await binding.repositories.sessions.get(active_cycle.session_id)
        if state is None or state.active_cycle_id != active_cycle.cycle_id:
            return None
        active_cycle.input_runtime_generation = state.generation
        self._drop_internal_resume_message(active_cycle)
        outcome = await service.run_checkpoint(checkpoint=checkpoint, active_cycle=active_cycle, desired_status=desired_status, waiting_question=waiting_question, interruption_reason=interruption_reason, apply_input=apply_input)
        if outcome.action == CheckpointAction.INTERRUPT:
            active_cycle.status = 'interrupted'
            active_cycle.interruption_reason = outcome.reason_code
        return outcome

    @staticmethod
    def _raise_if_interrupted(outcome: Any) -> None:
        if outcome is not None and outcome.action == CheckpointAction.INTERRUPT:
            raise RuntimeError(outcome.reason_code or 'input runtime interrupted')

    @staticmethod
    def _ir7_enabled() -> bool:
        binding = get_input_runtime_binding()
        assembler = get_final_output_assembler()
        return bool(
            binding is not None
            and binding.config.enabled
            and assembler is not None
            and getattr(getattr(assembler, 'config', None), 'enabled', False)
        )

    async def _capture_finalization_candidate(self, active_cycle: Any) -> Any:
        binding = get_input_runtime_binding()
        if binding is None:
            raise RuntimeError('input runtime binding missing during finalization')
        candidate = await binding.finalization_service.capture_candidate(
            session_id=active_cycle.session_id,
            cycle_id=active_cycle.cycle_id,
        )
        _checkpoint_finalization_candidate.set(candidate)
        return candidate

    async def _prepare_finalization(self, active_cycle: Any) -> None:
        if not self._ir7_enabled():
            return
        binding = get_input_runtime_binding()
        if binding is None:
            raise RuntimeError('input runtime binding missing during finalization')
        candidate = _checkpoint_finalization_candidate.get()
        if candidate is None:
            try:
                candidate = await self._capture_finalization_candidate(active_cycle)
            except InputRuntimeConflictError:
                self._remove_stale_candidate(active_cycle)
                self._trace_suppression(active_cycle, checkpoint=CheckpointName.BEFORE_TERMINAL_COMMIT)
                raise _SuppressStaleCandidate()
        preparation = await binding.finalization_service.prepare(candidate)
        record = preparation.record
        if record is None or record.state in {
            FinalizationState.ABORTED_NEW_INPUT,
            FinalizationState.ABORTED_CONTROL,
        }:
            self._remove_stale_candidate(active_cycle)
            self._trace_suppression(active_cycle, checkpoint=CheckpointName.BEFORE_TERMINAL_COMMIT)
            raise _SuppressStaleCandidate()
        _checkpoint_finalization_id.set(record.finalization_id)

    async def _commit_waiting_authority(self, active_cycle: Any, question: str) -> None:
        if not self._ir7_enabled():
            return
        binding = get_input_runtime_binding()
        if binding is None:
            raise RuntimeError('input runtime binding missing during waiting commit')
        state = await binding.repositories.sessions.get(active_cycle.session_id)
        if state is None:
            raise RuntimeError('waiting session state missing')
        try:
            await binding.finalization_service.commit_waiting(
                session_id=active_cycle.session_id,
                cycle_id=active_cycle.cycle_id,
                generation=state.generation,
                context_revision_id=state.active_context_revision_id or '',
                expected_input_sequence=state.active_cycle_applied_through_sequence,
                expected_control_sequence=state.applied_control_sequence,
                waiting_question=question,
            )
        except InputRuntimeConflictError as error:
            if str(error) not in {
                'waiting_aborted_new_input',
                'waiting_aborted_control',
                'waiting_snapshot_authority_changed',
            }:
                raise
            self._remove_stale_candidate(active_cycle)
            self._trace_suppression(active_cycle, checkpoint=CheckpointName.BEFORE_WAITING)
            raise _SuppressStaleCandidate()

    @staticmethod
    def _rollback_legacy_done_memory(owner: Any, active_cycle: Any, result: Any) -> None:
        session = owner._get_or_create_session(active_cycle.session_id)
        turns = getattr(session, 'dialog_turns', None)
        if turns:
            last = turns[-1]
            if getattr(last, 'final_answer', None) == getattr(result, 'content', None):
                turns.pop()
        session.pending_cycle = active_cycle

    async def _cleanup_aborted_output(self, record: Any) -> None:
        if not getattr(record, 'output_batch_id', None):
            return
        assembler = get_final_output_assembler()
        if assembler is None:
            return
        await abandon_uncommitted_final_output(
            assembler,
            output_batch_id=record.output_batch_id,
        )

    async def _suppress_after_durable_abort(self, active_cycle: Any, result: Any, record: Any) -> None:
        await self._cleanup_aborted_output(record)
        self._rollback_legacy_done_memory(self, active_cycle, result)
        binding = get_input_runtime_binding()
        state = (
            await binding.repositories.sessions.get(active_cycle.session_id)
            if binding is not None
            else None
        )
        if (
            record.state == FinalizationState.ABORTED_CONTROL
            and (
                state is None
                or state.generation != record.generation
                or state.active_cycle_id != record.cycle_id
            )
        ):
            raise RuntimeError(
                record.cancellation_reason_code or 'old_generation_finalization_aborted'
            )
        self._remove_stale_candidate(active_cycle)
        self._trace_suppression(active_cycle, checkpoint=CheckpointName.BEFORE_TERMINAL_COMMIT)
        raise _SuppressStaleCandidate()

    async def _complete_finalization(self, result: Any, active_cycle: Any) -> None:
        finalization_id = _checkpoint_finalization_id.get()
        if not self._ir7_enabled() or not finalization_id:
            return
        binding = get_input_runtime_binding()
        assembler = get_final_output_assembler()
        if binding is None or assembler is None:
            raise RuntimeError('IR-7 composition disappeared during finalization')
        if not getattr(result, 'cycle_id', None):
            result.cycle_id = active_cycle.cycle_id
        payload = result.model_dump(mode='json', exclude={'output_batch'})
        record = await binding.finalization_service.persist_result(
            finalization_id,
            payload,
        )
        if record.state in {
            FinalizationState.ABORTED_NEW_INPUT,
            FinalizationState.ABORTED_CONTROL,
        }:
            await self._suppress_after_durable_abort(active_cycle, result, record)
        input_batch_id = getattr(active_cycle, 'original_input_batch_id', None)
        if not input_batch_id:
            raise RuntimeError('finalization lost original InputBatch authority')
        input_batch = await binding.committed_batches.get_committed(input_batch_id)
        batch = await assembler.assemble_final(
            result=result,
            input_batch=input_batch,
            capability_snapshot=input_batch.capability_snapshot,
            locale=input_batch.locale,
        )
        record = await binding.finalization_service.mark_output_ready(
            finalization_id,
            output_batch_id=batch.output_batch_id,
        )
        if record.state in {
            FinalizationState.ABORTED_NEW_INPUT,
            FinalizationState.ABORTED_CONTROL,
        }:
            await self._suppress_after_durable_abort(active_cycle, result, record)
        record = await binding.finalization_service.terminal_commit(finalization_id)
        if record.state != FinalizationState.TERMINAL_COMMITTED:
            await self._suppress_after_durable_abort(active_cycle, result, record)

    async def _call_main_llm_with_context_recovery(self, **kwargs: Any):
        active_cycle = kwargs['active_cycle']
        _checkpoint_active_cycle.set(active_cycle)
        cycle_id = str(active_cycle.cycle_id)
        checkpoint = CheckpointName.RESUME if _checkpoint_restart.get() or cycle_id not in self._input_runtime_seen_cycles else CheckpointName.BEFORE_LLM
        if self._last_block_is_complete_tool_block(active_cycle.messages_for_llm):
            outcome = await self._run_input_checkpoint(CheckpointName.AFTER_TOOL_BLOCK, active_cycle=active_cycle, desired_status=CycleStatus.RUNNING)
            self._raise_if_interrupted(outcome)
        outcome = await self._run_input_checkpoint(checkpoint, active_cycle=active_cycle, desired_status=CycleStatus.RUNNING)
        self._input_runtime_seen_cycles.add(cycle_id)
        self._raise_if_interrupted(outcome)
        response, messages = await super()._call_main_llm_with_context_recovery(**kwargs)
        action = self._agent_action(response)
        if action is None:
            return response, messages
        if action.status == 'waiting_user':
            outcome = await self._run_input_checkpoint(CheckpointName.BEFORE_WAITING, active_cycle=active_cycle, desired_status=CycleStatus.RUNNING)
            self._raise_if_interrupted(outcome)
            if outcome is not None and outcome.action == CheckpointAction.INPUT_APPLIED:
                self._trace_suppression(active_cycle, checkpoint=CheckpointName.BEFORE_WAITING)
                return self._continue_response(response), messages
        if action.status == 'done':
            outcome = await self._run_input_checkpoint(CheckpointName.BEFORE_FINAL_PROCESSING, active_cycle=active_cycle, desired_status=CycleStatus.RUNNING)
            self._raise_if_interrupted(outcome)
            if outcome is not None and outcome.action == CheckpointAction.INPUT_APPLIED:
                self._trace_suppression(active_cycle, checkpoint=CheckpointName.BEFORE_FINAL_PROCESSING)
                return self._continue_response(response), messages
            if self._ir7_enabled():
                try:
                    await self._capture_finalization_candidate(active_cycle)
                except InputRuntimeConflictError:
                    self._trace_suppression(active_cycle, checkpoint=CheckpointName.BEFORE_FINAL_PROCESSING)
                    return self._continue_response(response), messages
        return response, messages

    async def _process_final_answer(self, **kwargs: Any) -> str:
        active_cycle = _checkpoint_active_cycle.get()
        outcome = await self._run_input_checkpoint(CheckpointName.BEFORE_FINAL_PROCESSING, active_cycle=active_cycle, desired_status=CycleStatus.RUNNING)
        self._raise_if_interrupted(outcome)
        if outcome is not None and outcome.action == CheckpointAction.INPUT_APPLIED:
            if active_cycle is not None:
                self._remove_stale_candidate(active_cycle)
                self._trace_suppression(active_cycle, checkpoint=CheckpointName.BEFORE_FINAL_PROCESSING)
            raise _SuppressStaleCandidate()
        if active_cycle is not None and self._ir7_enabled():
            try:
                await self._capture_finalization_candidate(active_cycle)
            except InputRuntimeConflictError:
                self._remove_stale_candidate(active_cycle)
                self._trace_suppression(active_cycle, checkpoint=CheckpointName.BEFORE_FINAL_PROCESSING)
                raise _SuppressStaleCandidate()
        return await super()._process_final_answer(**kwargs)

    async def _emit_progress_event(self, *args: Any, **kwargs: Any):
        event_type = kwargs.get('event_type')
        active_cycle = _checkpoint_active_cycle.get()
        if active_cycle is not None and event_type == 'waiting_user':
            candidate = self._last_candidate(active_cycle)
            question = candidate.question_to_user if candidate is not None else None
            outcome = await self._run_input_checkpoint(CheckpointName.BEFORE_WAITING, active_cycle=active_cycle, desired_status=CycleStatus.RUNNING, waiting_question=question)
            self._raise_if_interrupted(outcome)
            if outcome is not None and outcome.action == CheckpointAction.INPUT_APPLIED:
                self._remove_stale_candidate(active_cycle)
                self._trace_suppression(active_cycle, checkpoint=CheckpointName.BEFORE_WAITING)
                raise _SuppressStaleCandidate()
            if question and self._ir7_enabled():
                await self._commit_waiting_authority(active_cycle, question)
        if active_cycle is not None and event_type in {'cycle_done', 'cycle_error'}:
            desired_status = CycleStatus.DONE if event_type == 'cycle_done' else CycleStatus.ERROR
            outcome = await self._run_input_checkpoint(CheckpointName.BEFORE_TERMINAL_COMMIT, active_cycle=active_cycle, desired_status=desired_status)
            self._raise_if_interrupted(outcome)
            if outcome is not None and outcome.action == CheckpointAction.INPUT_APPLIED:
                self._remove_stale_candidate(active_cycle)
                self._trace_suppression(active_cycle, checkpoint=CheckpointName.BEFORE_TERMINAL_COMMIT)
                raise _SuppressStaleCandidate()
            if event_type == 'cycle_done':
                await self._prepare_finalization(active_cycle)
        return await super()._emit_progress_event(*args, **kwargs)

    def _prepare_same_cycle_restart(self, active_cycle: Any) -> None:
        active_cycle.status = 'waiting_user'
        active_cycle.waiting_question = 'runtime_checkpoint_continue'
        active_cycle.interruption_reason = None
        self._get_or_create_session(active_cycle.session_id).pending_cycle = active_cycle

    @staticmethod
    def _restart_arguments(args: tuple[Any, ...], kwargs: dict[str, Any], *, active_cycle: Any) -> tuple[tuple[Any, ...], dict[str, Any]]:
        forwarded = dict(kwargs)
        forwarded.pop('input_batch', None)
        if args:
            args = ('', *args[1:])
            forwarded.pop('query', None)
        else:
            forwarded['query'] = ''
        forwarded['cycle_id_override'] = active_cycle.cycle_id
        return args, forwarded

    async def process_query(self, *args: Any, **kwargs: Any):
        active_token = _checkpoint_active_cycle.set(None)
        candidate_token = _checkpoint_finalization_candidate.set(None)
        finalization_token = _checkpoint_finalization_id.set(None)
        current_args, current_kwargs, restarting = args, dict(kwargs), False
        try:
            while True:
                restart_token = _checkpoint_restart.set(restarting)
                try:
                    try:
                        result = await super().process_query(*current_args, **current_kwargs)
                        active_cycle = _checkpoint_active_cycle.get()
                        status = str(getattr(result.status, 'value', result.status))
                        if active_cycle is not None and status == 'done':
                            await self._complete_finalization(result, active_cycle)
                    except _SuppressStaleCandidate:
                        active_cycle = _checkpoint_active_cycle.get()
                        if active_cycle is None:
                            raise RuntimeError('checkpoint continuation lost active cycle')
                        self._prepare_same_cycle_restart(active_cycle)
                        _checkpoint_finalization_candidate.set(None)
                        _checkpoint_finalization_id.set(None)
                        current_args, current_kwargs = self._restart_arguments(current_args, current_kwargs, active_cycle=active_cycle)
                        restarting = True
                        continue
                finally:
                    _checkpoint_restart.reset(restart_token)
                active_cycle = _checkpoint_active_cycle.get()
                status = str(getattr(result.status, 'value', result.status))
                if active_cycle is not None and status == 'error' and bool(getattr(result, 'can_resume', False)):
                    outcome = await self._run_input_checkpoint(CheckpointName.AFTER_INTERRUPTION, active_cycle=active_cycle, desired_status=CycleStatus.INTERRUPTED, interruption_reason=getattr(active_cycle, 'interruption_reason', None) or str(getattr(result, 'content', '') or '') or 'runtime_interrupted', apply_input=False)
                    self._raise_if_interrupted(outcome)
                return result
        finally:
            _checkpoint_finalization_id.reset(finalization_token)
            _checkpoint_finalization_candidate.reset(candidate_token)
            _checkpoint_active_cycle.reset(active_token)
