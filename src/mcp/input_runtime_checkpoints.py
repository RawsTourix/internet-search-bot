"""Minimal IR-4 checkpoint hooks around the existing agent loop."""

from __future__ import annotations

import json
from contextvars import ContextVar
from typing import Any

from ..input_runtime import (
    CheckpointAction,
    CheckpointName,
    CycleInputApplier,
    InputRuntimeCheckpointService,
    get_input_runtime_binding,
)
from .manager_runtime_context import get_manager_context


_checkpoint_restart: ContextVar[bool] = ContextVar(
    "input_runtime_checkpoint_restart", default=False
)


class _SuppressStaleCandidate(BaseException):
    pass


class InputRuntimeCheckpointMixin:
    """Delegate safe checkpoint work without repository logic in MCPClient."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._input_runtime_checkpoint_service = None
        self._input_runtime_seen_cycles: set[str] = set()
        super().__init__(*args, **kwargs)

    def _checkpoint_service(self):
        binding = get_input_runtime_binding()
        if binding is None or not binding.config.enabled:
            return None
        if self._input_runtime_checkpoint_service is None:
            self._input_runtime_checkpoint_service = InputRuntimeCheckpointService(
                applier=CycleInputApplier(
                    config=binding.config,
                    repositories=binding.repositories,
                    committed_batches=binding.committed_batches,
                )
            )
        return self._input_runtime_checkpoint_service

    @staticmethod
    def _last_block_is_complete_tool_block(messages: list[dict[str, Any]]) -> bool:
        if not messages or messages[-1].get("role") != "tool":
            return False
        index = len(messages) - 1
        result_ids: list[str] = []
        while index >= 0 and messages[index].get("role") == "tool":
            result_ids.append(str(messages[index].get("tool_call_id") or ""))
            index -= 1
        if index < 0:
            return False
        assistant = messages[index]
        calls = assistant.get("tool_calls")
        if assistant.get("role") != "assistant" or not isinstance(calls, list):
            return False
        call_ids = [str(item.get("id") or "") for item in calls]
        return (
            bool(call_ids)
            and len(call_ids) == len(set(call_ids))
            and len(result_ids) == len(set(result_ids))
            and set(call_ids) == set(result_ids)
        )

    @staticmethod
    def _drop_internal_resume_message(active_cycle: Any) -> None:
        if not _checkpoint_restart.get() or not active_cycle.messages_for_llm:
            return
        message = active_cycle.messages_for_llm[-1]
        if message.get("role") != "user":
            return
        try:
            payload = json.loads(message.get("content") or "")
        except Exception:
            return
        if (
            isinstance(payload, dict)
            and payload.get("type") == "user_reply_during_waiting_user"
            and not str(payload.get("reply") or "").strip()
        ):
            active_cycle.messages_for_llm.pop()
            if active_cycle.cycle_trace:
                last = active_cycle.cycle_trace[-1]
                if (
                    last.get("type") == "user_reply_during_waiting_user"
                    and not str(last.get("reply") or "").strip()
                ):
                    active_cycle.cycle_trace.pop()

    async def _run_input_checkpoint(
        self,
        checkpoint: CheckpointName,
        *,
        active_cycle: Any | None = None,
    ):
        service = self._checkpoint_service()
        context = get_manager_context()
        active_cycle = active_cycle or (
            context.active_cycle if context is not None else None
        )
        if service is None or active_cycle is None:
            return None
        binding = get_input_runtime_binding()
        state = await binding.repositories.sessions.get(active_cycle.session_id)
        if state is None or state.active_cycle_id != active_cycle.cycle_id:
            return None
        active_cycle.input_runtime_generation = state.generation
        self._drop_internal_resume_message(active_cycle)
        outcome = await service.run_checkpoint(
            checkpoint=checkpoint,
            active_cycle=active_cycle,
        )
        if outcome.action == CheckpointAction.INTERRUPT:
            active_cycle.status = "interrupted"
            active_cycle.interruption_reason = outcome.reason_code
        return outcome

    async def _call_main_llm_with_context_recovery(self, **kwargs: Any):
        active_cycle = kwargs["active_cycle"]
        cycle_id = str(active_cycle.cycle_id)
        checkpoint = (
            CheckpointName.RESUME
            if cycle_id not in self._input_runtime_seen_cycles
            else CheckpointName.BEFORE_LLM
        )
        if self._last_block_is_complete_tool_block(active_cycle.messages_for_llm):
            outcome = await self._run_input_checkpoint(
                CheckpointName.AFTER_TOOL_BLOCK,
                active_cycle=active_cycle,
            )
            if outcome is not None and outcome.action == CheckpointAction.INTERRUPT:
                raise RuntimeError(outcome.reason_code or "input runtime interrupted")
        outcome = await self._run_input_checkpoint(
            checkpoint, active_cycle=active_cycle
        )
        self._input_runtime_seen_cycles.add(cycle_id)
        if outcome is not None and outcome.action == CheckpointAction.INTERRUPT:
            raise RuntimeError(outcome.reason_code or "input runtime interrupted")
        return await super()._call_main_llm_with_context_recovery(**kwargs)

    async def _process_final_answer(self, **kwargs: Any) -> str:
        outcome = await self._run_input_checkpoint(
            CheckpointName.BEFORE_FINAL_PROCESSING
        )
        if outcome is not None and outcome.action == CheckpointAction.INPUT_APPLIED:
            raise _SuppressStaleCandidate()
        if outcome is not None and outcome.action == CheckpointAction.INTERRUPT:
            raise RuntimeError(outcome.reason_code or "input runtime interrupted")
        return await super()._process_final_answer(**kwargs)

    async def _emit_progress_event(self, *args: Any, **kwargs: Any):
        if kwargs.get("event_type") == "waiting_user":
            context = get_manager_context()
            active_cycle = context.active_cycle if context is not None else None
            if active_cycle is not None and active_cycle.messages_for_llm:
                stale_message = active_cycle.messages_for_llm[-1]
                outcome = await self._run_input_checkpoint(
                    CheckpointName.BEFORE_WAITING,
                    active_cycle=active_cycle,
                )
                if (
                    outcome is not None
                    and outcome.action == CheckpointAction.INPUT_APPLIED
                ):
                    if active_cycle.messages_for_llm:
                        candidate = active_cycle.messages_for_llm[-1]
                        if candidate is stale_message:
                            active_cycle.messages_for_llm.pop()
                    raise _SuppressStaleCandidate()
                if outcome is not None and outcome.action == CheckpointAction.INTERRUPT:
                    raise RuntimeError(
                        outcome.reason_code or "input runtime interrupted"
                    )
        return await super()._emit_progress_event(*args, **kwargs)

    async def process_query(self, *args: Any, **kwargs: Any):
        try:
            result = await super().process_query(*args, **kwargs)
        except _SuppressStaleCandidate:
            context = get_manager_context()
            if context is None:
                raise RuntimeError("checkpoint continuation lost active cycle")
            active_cycle = context.active_cycle
            active_cycle.status = "waiting_user"
            active_cycle.waiting_question = "runtime_checkpoint_continue"
            self._get_or_create_session(active_cycle.session_id).pending_cycle = (
                active_cycle
            )
            token = _checkpoint_restart.set(True)
            try:
                forwarded = dict(kwargs)
                forwarded.pop("input_batch", None)
                if args:
                    args = ("", *args[1:])
                else:
                    forwarded["query"] = ""
                forwarded["cycle_id_override"] = active_cycle.cycle_id
                return await self.process_query(*args, **forwarded)
            finally:
                _checkpoint_restart.reset(token)

        context = get_manager_context()
        active_cycle = context.active_cycle if context is not None else None
        if active_cycle is None:
            return result
        status = str(getattr(result.status, "value", result.status))
        if status in {"done", "error"}:
            checkpoint = (
                CheckpointName.AFTER_INTERRUPTION
                if status == "error" and bool(getattr(result, "can_resume", False))
                else CheckpointName.BEFORE_TERMINAL_COMMIT
            )
            outcome = await self._run_input_checkpoint(
                checkpoint, active_cycle=active_cycle
            )
            if outcome is not None and outcome.action == CheckpointAction.INPUT_APPLIED:
                active_cycle.status = "waiting_user"
                active_cycle.waiting_question = "runtime_checkpoint_continue"
                self._get_or_create_session(active_cycle.session_id).pending_cycle = (
                    active_cycle
                )
                token = _checkpoint_restart.set(True)
                try:
                    forwarded = dict(kwargs)
                    forwarded.pop("input_batch", None)
                    if args:
                        args = ("", *args[1:])
                    else:
                        forwarded["query"] = ""
                    forwarded["cycle_id_override"] = active_cycle.cycle_id
                    return await self.process_query(*args, **forwarded)
                finally:
                    _checkpoint_restart.reset(token)
        return result
