"""Infrastructure-safe planning manager controller."""

from __future__ import annotations

from ..mcp.manager_context import ManagerToolContext
from .errors import (
    PlanAccessError,
    PlanNotFoundError,
    PlanStorageError,
    PlanValidationError,
)
from .models import ActivePlanState
from .tools import PlanningToolController


class SafePlanningToolController(PlanningToolController):
    """Refresh stale state without converting storage failures into null state."""

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
