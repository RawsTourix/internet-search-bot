"""Production hardening around the planning-aware MCP client."""

from __future__ import annotations

from typing import Any

from ..agent.protocol import AgentAction
from ..core.models import AgentStatus
from ..planning import AgentActivity, PlanConsistencyError
from ..planning.safe_tools import SafePlanningToolController
from .manager_context import ManagerToolContext
from .planning_client import PlanningMCPClient


class FinalizingPlanningMCPClient(PlanningMCPClient):
    """Apply production guards around planning-aware agent execution."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.plan_tool_controller = SafePlanningToolController(
            self.planning_services.planning_service
        )

    async def process_query(self, *args: Any, **kwargs: Any):
        """Keep AgentResult.can_resume aligned with the saved pending cycle.

        The stable base runtime already persists an ActiveAgentCycle when an
        AgentAction enters WAITING_USER. Historically AgentResult.can_resume was
        populated only for infrastructure interruptions, so a normal user
        handoff incorrectly reported ``False`` despite having resumable state.
        """

        result = await super().process_query(*args, **kwargs)
        if result.status == AgentStatus.WAITING_USER:
            session_id = result.session_id or "default"
            session = self._get_or_create_session(session_id)
            result.can_resume = session.pending_cycle is not None
        return result

    async def _manager_get_tool_schema(
        self,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Return schemas for built-in manager tools as well as remote tools."""

        tool_name = str(arguments["tool_name"])
        manager_spec = self.manager_tools.get(tool_name)
        if manager_spec is not None:
            return {
                "type": "mcp_tool_schema",
                "tool": {
                    "name": manager_spec.name,
                    "description": manager_spec.description,
                    "inputSchema": manager_spec.parameters,
                    "source": "manager",
                },
            }
        return await super()._manager_get_tool_schema(arguments)

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
                question = (
                    "The task could not be finalized safely because the active "
                    "work plan still contains unresolved stages. The plan and "
                    "collected runtime state were preserved. Continue working "
                    "through the active plan?"
                )
            else:
                question = (
                    "Задачу не удалось безопасно финализировать: в активном плане "
                    "остались незавершённые этапы. План и собранное состояние "
                    "сохранены. Продолжить выполнение активного плана?"
                )

            result = dict(response)
            result["content"] = AgentAction(
                status="waiting_user",
                action="ask_user",
                question_to_user=question,
            ).model_dump_json()
            result["tool_calls"] = []
            return result
