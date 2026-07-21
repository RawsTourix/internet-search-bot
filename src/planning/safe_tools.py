"""Infrastructure-safe planning manager controller."""

from __future__ import annotations

from typing import Any

from ..mcp.manager_context import ManagerToolContext
from .errors import (
    PlanAccessError,
    PlanNotFoundError,
    PlanStorageError,
    PlanValidationError,
)
from .models import ActivePlanState
from .tools import PlanningToolController, PlanningToolOutcome


_MUTATION_SUCCESS_TYPES = frozenset({
    "plan_created",
    "plan_nodes_added",
    "plan_node_updated",
    "plan_node_transitioned",
    "plan_node_removed",
    "plan_cancelled",
})


class SafePlanningToolController(PlanningToolController):
    """Keep infrastructure errors visible and reconciliation accounting strict."""

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ManagerToolContext,
    ) -> PlanningToolOutcome:
        outcome = await super().execute(tool_name, arguments, context)
        if outcome.payload.get("type") in _MUTATION_SUCCESS_TYPES:
            context.active_cycle.plan_reconciliation_attempts = 0
        return outcome

    @staticmethod
    def _sync_cycle(
        context: ManagerToolContext,
        state: ActivePlanState,
    ) -> None:
        """Refresh the projection without treating reads as reconciliation."""
        cycle = context.active_cycle
        cycle.active_plan_id = state.plan_id
        cycle.active_plan_revision = state.revision
        cycle.active_plan_node_id = (
            state.current_node.node_id if state.current_node else None
        )
        cycle.active_plan_state = state

    async def _refresh_after_conflict(
        self,
        plan_id: str,
        context: ManagerToolContext,
    ) -> ActivePlanState | None:
        try:
            state = await self.service.get_active_state(
                session_id=context.session_id,
                cycle_id=context.cycle_id,
                plan_id=plan_id,
            )
        except PlanStorageError:
            raise
        except (PlanNotFoundError, PlanAccessError, PlanValidationError):
            return None
        if context.active_cycle.active_plan_id == state.plan_id:
            self._sync_cycle(context, state)
        return state
