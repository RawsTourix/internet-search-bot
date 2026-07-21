"""Backend-independent planning persistence contracts."""

from typing import Protocol, runtime_checkable

from .models import AgentPlan, PlanRef


@runtime_checkable
class PlanStore(Protocol):
    """Exact revisioned persistence for current DAG plan state."""

    async def create_plan(self, plan: AgentPlan) -> AgentPlan:
        ...

    async def get_plan(
        self,
        plan_id: str,
        *,
        revision: int | None = None,
    ) -> AgentPlan:
        ...

    async def save_revision(
        self,
        plan: AgentPlan,
        *,
        expected_revision: int,
    ) -> AgentPlan:
        ...

    async def list_cycle_plans(self, cycle_id: str) -> list[PlanRef]:
        ...
