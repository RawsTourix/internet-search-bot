"""Same-process hardening for stale checkpoint candidate suppression."""

from __future__ import annotations

import json
from typing import Any

from ..agent.protocol import AgentAction
from ..input_runtime import (
    CheckpointAction,
    CheckpointName,
    CheckpointOutcome,
    CycleStatus,
    get_input_runtime_binding,
)
from .input_runtime_checkpoints import (
    _SuppressStaleCandidate,
    _checkpoint_active_cycle,
)


class InputRuntimeCheckpointHardeningMixin:
    """Fence authority, suppress stale candidates, and order terminal sync."""

    @staticmethod
    def _is_input_batch_update(message: dict[str, Any]) -> bool:
        if message.get("role") != "user":
            return False
        content = message.get("content")
        if not isinstance(content, str):
            return False
        try:
            payload = json.loads(content)
        except Exception:
            return False
        return (
            isinstance(payload, dict)
            and payload.get("type") == "input_batch_update"
            and payload.get("runtime_generated") is True
        )

    @classmethod
    def _candidate_entry(
        cls,
        active_cycle: Any,
    ) -> tuple[int, AgentAction] | None:
        for index in range(
            len(active_cycle.messages_for_llm) - 1,
            -1,
            -1,
        ):
            message = active_cycle.messages_for_llm[index]
            if cls._is_input_batch_update(message):
                continue
            if (
                message.get("role") != "assistant"
                or message.get("tool_calls")
            ):
                return None
            content = message.get("content")
            if not isinstance(content, str):
                return None
            try:
                return index, AgentAction.model_validate_json(content)
            except Exception:
                return None
        return None

    @classmethod
    def _last_candidate(cls, active_cycle: Any) -> AgentAction | None:
        entry = cls._candidate_entry(active_cycle)
        return entry[1] if entry is not None else None

    @classmethod
    def _remove_stale_candidate(cls, active_cycle: Any) -> None:
        entry = cls._candidate_entry(active_cycle)
        if (
            entry is None
            or entry[1].status not in {"waiting_user", "done", "error"}
        ):
            return
        active_cycle.messages_for_llm.pop(entry[0])

    async def _run_input_checkpoint(
        self,
        checkpoint: CheckpointName,
        *,
        active_cycle: Any | None = None,
        desired_status: CycleStatus | None = None,
        waiting_question: str | None = None,
        interruption_reason: str | None = None,
        apply_input: bool = True,
        terminal_sync: bool = False,
    ):
        service = self._checkpoint_service()
        active_cycle = active_cycle or _checkpoint_active_cycle.get()
        if service is None or active_cycle is None:
            return None
        binding = get_input_runtime_binding()
        if binding is None:
            return CheckpointOutcome(
                checkpoint=checkpoint,
                action=CheckpointAction.INTERRUPT,
                reason_code="input_runtime_binding_missing",
            )
        state = await binding.repositories.sessions.get(active_cycle.session_id)
        if state is None:
            return CheckpointOutcome(
                checkpoint=checkpoint,
                action=CheckpointAction.INTERRUPT,
                reason_code="checkpoint_session_state_missing",
            )
        if state.active_cycle_id != active_cycle.cycle_id:
            return CheckpointOutcome(
                checkpoint=checkpoint,
                action=CheckpointAction.INTERRUPT,
                reason_code="checkpoint_active_cycle_mismatch",
            )
        runner_generation = int(
            getattr(active_cycle, "input_runtime_generation", -1)
        )
        if runner_generation != state.generation:
            return CheckpointOutcome(
                checkpoint=checkpoint,
                action=CheckpointAction.INTERRUPT,
                reason_code="checkpoint_runner_generation_stale",
            )
        if (
            checkpoint == CheckpointName.BEFORE_TERMINAL_COMMIT
            and desired_status in {CycleStatus.DONE, CycleStatus.ERROR}
            and not terminal_sync
        ):
            desired_status = CycleStatus.RUNNING
        self._drop_internal_resume_message(active_cycle)
        outcome = await service.run_checkpoint(
            checkpoint=checkpoint,
            active_cycle=active_cycle,
            desired_status=desired_status,
            waiting_question=waiting_question,
            interruption_reason=interruption_reason,
            apply_input=apply_input,
            terminal_sync=terminal_sync,
        )
        if outcome.action == CheckpointAction.INTERRUPT:
            active_cycle.status = "interrupted"
            active_cycle.interruption_reason = outcome.reason_code
        return outcome

    async def _persist_suppressed_candidate(
        self,
        *,
        checkpoint: CheckpointName,
        active_cycle: Any,
    ) -> None:
        outcome = await self._run_input_checkpoint(
            checkpoint,
            active_cycle=active_cycle,
            desired_status=CycleStatus.RUNNING,
            apply_input=False,
        )
        self._raise_if_interrupted(outcome)

    async def _process_final_answer(self, **kwargs: Any) -> str:
        try:
            return await super()._process_final_answer(**kwargs)
        except _SuppressStaleCandidate:
            active_cycle = _checkpoint_active_cycle.get()
            if active_cycle is not None:
                await self._persist_suppressed_candidate(
                    checkpoint=CheckpointName.BEFORE_FINAL_PROCESSING,
                    active_cycle=active_cycle,
                )
            raise

    async def _emit_progress_event(self, *args: Any, **kwargs: Any):
        event_type = kwargs.get("event_type")
        active_cycle = _checkpoint_active_cycle.get()
        checkpoint = None
        if event_type == "waiting_user":
            checkpoint = CheckpointName.BEFORE_WAITING
        elif event_type in {"cycle_done", "cycle_error"}:
            checkpoint = CheckpointName.BEFORE_TERMINAL_COMMIT

        try:
            result = await super()._emit_progress_event(*args, **kwargs)
        except _SuppressStaleCandidate:
            if checkpoint is not None and active_cycle is not None:
                await self._persist_suppressed_candidate(
                    checkpoint=checkpoint,
                    active_cycle=active_cycle,
                )
            raise

        # Terminal status is synchronized by InputAdmissionService only after
        # the existing status/output compatibility operations have succeeded.
        return result
