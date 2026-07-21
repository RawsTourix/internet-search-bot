"""Production hardening around the planning-aware MCP client."""

from __future__ import annotations

from typing import Any

from ..agent.protocol import AgentAction
from ..planning import AgentActivity, PlanConsistencyError
from ..planning.safe_tools import SafePlanningToolController
from .manager_context import ManagerToolContext
from .planning_client import PlanningMCPClient


class FinalizingPlanningMCPClient(PlanningMCPClient):
    """Convert repeated plan-protocol violations into an honest partial answer."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.plan_tool_controller = SafePlanningToolController(
            self.planning_services.planning_service
        )

    async def _apply_plan_action_guard(
        self,
        *,
        response: dict[str, Any],
        context: ManagerToolContext,
    ) -> dict[str, Any]:
        try:
            return await super()._apply_plan_action_guard(
                response=response,
                context=context,
            )
        except PlanConsistencyError:
            cycle = context.active_cycle
            cycle.activity = AgentActivity.FINALIZING
            state = cycle.active_plan_state
            event_data = {
                "plan_id": state.plan_id if state is not None else None,
                "revision": state.revision if state is not None else None,
                "attempt": cycle.plan_reconciliation_attempts,
                "reason": "reconciliation_attempts_exhausted",
            }
            self._trace_event(
                cycle.cycle_trace,
                "plan_consistency_error",
                **event_data,
            )
            await self._emit_progress_event(
                state=context.session_state,
                session_id=context.session_id,
                cycle_id=context.cycle_id,
                progress_callback=context.progress_callback,
                cycle_trace=cycle.cycle_trace,
                event_type="plan_finalization_blocked",
                severity="error",
                visibility="user",
                data=event_data,
            )

            locale_name = str(
                getattr(context.session_state, "progress_locale", "ru") or "ru"
            ).lower()
            if locale_name.startswith("en"):
                final_answer = (
                    "The task could not be completed safely because the agent "
                    "repeatedly tried to finish while the active work plan still "
                    "contained unresolved stages. The plan and collected runtime "
                    "state were preserved for a later continuation."
                )
            else:
                final_answer = (
                    "Задачу не удалось безопасно завершить: агент несколько раз "
                    "попытался сформировать финальный ответ, пока в активном плане "
                    "оставались незавершённые этапы. План и собранное состояние "
                    "сохранены для последующего продолжения."
                )

            result = dict(response)
            result["content"] = AgentAction(
                status="done",
                action="answer",
                final_answer=final_answer,
            ).model_dump_json()
            result["tool_calls"] = []
            return result
