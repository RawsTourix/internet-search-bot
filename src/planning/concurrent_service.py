"""Concurrency hardening for planning domain operations."""

from __future__ import annotations

import asyncio

from .models import AgentPlan, CreatePlanNodeInput
from .service import PlanningService


class ConcurrentPlanningService(PlanningService):
    """Prevent two active plans from being created in one cycle concurrently."""

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
