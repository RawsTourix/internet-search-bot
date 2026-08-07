"""Cooperative IR-5 pause/reset fencing around the existing MCP agent loop."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.models import AgentResult, AgentStatus
from ..input_runtime import CheckpointAction, CheckpointName, CycleStatus, get_input_runtime_binding
from .input_runtime_checkpoints import _checkpoint_active_cycle


@dataclass(slots=True)
class _ControlUnwind(BaseException):
    active_cycle: Any
    reason_code: str
    action: CheckpointAction


class InputRuntimeControlMixin:
    """Unwind only at protocol-safe checkpoints; never cancel an atomic await."""

    def _raise_if_interrupted(self, outcome: Any) -> None:
        if outcome is None:
            return
        action = outcome.action
        if action in {CheckpointAction.PAUSE, CheckpointAction.WAIT}:
            active_cycle = _checkpoint_active_cycle.get()
            if active_cycle is None:
                raise RuntimeError("control checkpoint lost active cycle")
            raise _ControlUnwind(
                active_cycle=active_cycle,
                reason_code=outcome.reason_code or action.value,
                action=action,
            )
        if action == CheckpointAction.INTERRUPT and outcome.reason_code in {
            "checkpoint_runner_generation_stale",
            "checkpoint_active_cycle_mismatch",
            "checkpoint_cycle_authority_mismatch",
            "reset_generation_transition_pending",
        }:
            active_cycle = _checkpoint_active_cycle.get()
            if active_cycle is None:
                raise RuntimeError(outcome.reason_code or "runtime authority lost")
            raise _ControlUnwind(
                active_cycle=active_cycle,
                reason_code=outcome.reason_code or "runtime_authority_fenced",
                action=CheckpointAction.INTERRUPT,
            )
        return super()._raise_if_interrupted(outcome)

    async def _call_main_llm_with_context_recovery(self, **kwargs: Any):
        """Add the post-LLM/pre-tool control boundary.

        A stop accepted while the bounded LLM request is in flight cannot cancel
        that request.  Once it returns, controls are observed before its tool
        calls can begin.  A multi-tool block is still completed by the existing
        loop and is checked at CP-AFTER-TOOL-BLOCK before another LLM request.
        """
        response, messages = await super()._call_main_llm_with_context_recovery(
            **kwargs
        )
        active_cycle = kwargs["active_cycle"]
        outcome = await self._run_input_checkpoint(
            CheckpointName.BEFORE_LLM,
            active_cycle=active_cycle,
            desired_status=CycleStatus.RUNNING,
            apply_input=False,
        )
        self._raise_if_interrupted(outcome)
        return response, messages

    async def _install_control_pending_cycle(
        self,
        unwind: _ControlUnwind,
    ) -> Any:
        active_cycle = unwind.active_cycle
        binding = get_input_runtime_binding()
        snapshot = None
        if binding is not None:
            snapshot = await binding.repositories.snapshots.get(
                str(active_cycle.cycle_id)
            )
        if snapshot is not None:
            active_cycle.messages_for_llm = list(snapshot.messages_for_llm)
            active_cycle.cycle_trace = list(snapshot.cycle_trace)
            active_cycle.active_context_revision_id = (
                snapshot.active_context_revision_id
            )
            active_cycle.applied_input_batch_ids = list(
                snapshot.applied_input_batch_ids
            )
            active_cycle.applied_through_cycle_sequence = (
                snapshot.applied_through_cycle_sequence
            )
            active_cycle.input_runtime_generation = snapshot.generation
            active_cycle.waiting_question = snapshot.waiting_question
            active_cycle.interruption_reason = snapshot.interruption_reason
            active_cycle.input_runtime_safe_checkpoint = (
                snapshot.safe_checkpoint.value
            )
            active_cycle.input_runtime_snapshot_revision = (
                snapshot.snapshot_revision
            )
            active_cycle.artifact_refs = list(snapshot.artifact_refs)
            active_cycle.read_artifact_refs = list(snapshot.read_artifact_refs)
            active_cycle.result_refs = list(snapshot.result_refs)
            active_cycle.active_plan_id = snapshot.active_plan_id
            active_cycle.active_plan_revision = snapshot.active_plan_revision
            active_cycle.active_plan_node_id = snapshot.active_plan_node_id
        if unwind.action == CheckpointAction.PAUSE:
            active_cycle.status = CycleStatus.PAUSED_BY_USER.value
        elif unwind.action == CheckpointAction.WAIT:
            active_cycle.status = CycleStatus.WAITING_USER.value
        else:
            active_cycle.status = "interrupted"
            active_cycle.interruption_reason = unwind.reason_code
        active_cycle.updated_at = self._now_timestamp()
        if unwind.action in {CheckpointAction.PAUSE, CheckpointAction.WAIT}:
            self._get_or_create_session(active_cycle.session_id).pending_cycle = (
                active_cycle
            )
        return active_cycle

    @staticmethod
    def _now_timestamp() -> float:
        import time

        return time.time()

    def _control_result(self, unwind: _ControlUnwind, active_cycle: Any) -> AgentResult:
        state = self._get_or_create_state(active_cycle.session_id)
        if unwind.action == CheckpointAction.WAIT:
            state.status = AgentStatus.WAITING_USER
            state.awaiting_user_input = True
            content = active_cycle.waiting_question or "input_runtime.control.waiting"
            can_resume = True
        elif unwind.action == CheckpointAction.PAUSE:
            # AgentStatus predates IR-5 PAUSED. RUNNING is a compatibility shell;
            # durable status remains paused_by_user and is protected from result
            # mapping by InputAdmissionService.record_cycle_status().
            state.status = AgentStatus.RUNNING
            state.awaiting_user_input = False
            content = "input_runtime.control.paused"
            can_resume = True
        else:
            state.status = AgentStatus.ERROR
            state.awaiting_user_input = False
            content = "input_runtime.control.fenced"
            can_resume = False
        return AgentResult(
            content=content,
            status=state.status,
            session_id=active_cycle.session_id,
            cycle_id=active_cycle.cycle_id,
            iterations=state.iterations,
            tools_used=list(state.tools_used),
            error=(unwind.reason_code if unwind.action == CheckpointAction.INTERRUPT else None),
            error_kind=("runtime_control_fence" if unwind.action == CheckpointAction.INTERRUPT else None),
            can_resume=can_resume,
            progress_events=list(state.progress_events),
        )

    async def process_query(self, *args: Any, **kwargs: Any):
        session_id = str(kwargs.get("session_id") or "default")
        pending = self._get_or_create_session(session_id).pending_cycle
        if pending is not None and str(getattr(pending, "status", "")) in {
            CycleStatus.PAUSED_BY_USER.value,
            CycleStatus.INTERRUPTED.value,
        }:
            # A true control resume is a fresh CP-RESUME boundary for the same
            # durable cycle even if this process saw the cycle before pausing.
            self._input_runtime_seen_cycles.discard(str(pending.cycle_id))
        try:
            return await super().process_query(*args, **kwargs)
        except _ControlUnwind as unwind:
            active_cycle = await self._install_control_pending_cycle(unwind)
            return self._control_result(unwind, active_cycle)
