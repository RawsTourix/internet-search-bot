"""Concurrency and optimistic-revision hardening for planning operations."""

from __future__ import annotations

import asyncio
from typing import Any

from .errors import PlanRevisionConflictError
from .models import AgentPlan, CreatePlanNodeInput
from .service import PlanningService


class ConcurrentPlanningService(PlanningService):
    """Harden plan creation and mutation ordering within one process."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._cycle_create_locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def create_plan(
        self,
        *,
        session_id: str,
        cycle_id: str,
        goal: str,
        strategy: str | None,
        nodes: list[CreatePlanNodeInput],
    ) -> tuple[AgentPlan, dict[str, str]]:
        key = (session_id, cycle_id)
        lock = self._cycle_create_locks.setdefault(key, asyncio.Lock())
        async with lock:
            return await super().create_plan(
                session_id=session_id,
                cycle_id=cycle_id,
                goal=goal,
                strategy=strategy,
                nodes=nodes,
            )

    async def add_nodes(self, **kwargs: Any):
        await self._preflight_revision(**self._revision_args(kwargs))
        return await super().add_nodes(**kwargs)

    async def update_node(self, **kwargs: Any):
        await self._preflight_revision(**self._revision_args(kwargs))
        return await super().update_node(**kwargs)

    async def transition_node(self, **kwargs: Any):
        await self._preflight_revision(**self._revision_args(kwargs))
        return await super().transition_node(**kwargs)

    async def remove_node(self, **kwargs: Any):
        await self._preflight_revision(**self._revision_args(kwargs))
        return await super().remove_node(**kwargs)

    async def cancel_plan(self, **kwargs: Any):
        await self._preflight_revision(**self._revision_args(kwargs))
        return await super().cancel_plan(**kwargs)

    async def _preflight_revision(
        self,
        *,
        session_id: str,
        cycle_id: str,
        plan_id: str,
        expected_revision: int,
    ) -> None:
        plan = await self.get_plan(
            session_id=session_id,
            cycle_id=cycle_id,
            plan_id=plan_id,
        )
        if plan.revision != expected_revision:
            raise PlanRevisionConflictError(
                plan_id,
                expected_revision=expected_revision,
                current_revision=plan.revision,
            )

    @staticmethod
    def _revision_args(kwargs: dict[str, Any]) -> dict[str, Any]:
        return {
            "session_id": kwargs["session_id"],
            "cycle_id": kwargs["cycle_id"],
            "plan_id": kwargs["plan_id"],
            "expected_revision": kwargs["expected_revision"],
        }
