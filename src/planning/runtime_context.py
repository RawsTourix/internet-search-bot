"""Task-local planning context shared by manager tools and storage adapters."""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any

from ..mcp.manager_context import ManagerToolContext
from ..storage.interfaces import ContentStore


_CURRENT_MANAGER_CONTEXT: ContextVar[ManagerToolContext | None] = ContextVar(
    "planning_manager_context",
    default=None,
)


def get_manager_context() -> ManagerToolContext | None:
    return _CURRENT_MANAGER_CONTEXT.get()


def set_manager_context(
    context: ManagerToolContext | None,
) -> Token[ManagerToolContext | None]:
    return _CURRENT_MANAGER_CONTEXT.set(context)


def reset_manager_context(token: Token[ManagerToolContext | None]) -> None:
    _CURRENT_MANAGER_CONTEXT.reset(token)


class PlanningAwareContentStore(ContentStore):
    """Decorate ContentStore metadata with authoritative active-plan identity."""

    def __init__(self, wrapped: ContentStore) -> None:
        self.wrapped = wrapped

    async def save_content(
        self,
        content: bytes | str,
        *,
        source_type: str,
        source_name: str | None = None,
        mime_type: str | None = None,
        encoding: str | None = None,
        cycle_id: str | None = None,
        tool_call_id: str | None = None,
        size_tokens_estimate: int | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        enriched = dict(metadata or {})
        context = get_manager_context()
        if context is not None:
            cycle = context.active_cycle
            if cycle.active_plan_id is not None:
                enriched.update({
                    "plan_id": cycle.active_plan_id,
                    "plan_revision": cycle.active_plan_revision,
                    "plan_node_id": cycle.active_plan_node_id,
                    "agent_activity": (
                        cycle.activity.value if cycle.activity is not None else None
                    ),
                })
        return await self.wrapped.save_content(
            content,
            source_type=source_type,
            source_name=source_name,
            mime_type=mime_type,
            encoding=encoding,
            cycle_id=cycle_id,
            tool_call_id=tool_call_id,
            size_tokens_estimate=size_tokens_estimate,
            metadata=enriched,
        )

    async def get_metadata(self, content_id: str):
        return await self.wrapped.get_metadata(content_id)

    async def read_content(self, content_id: str) -> bytes:
        return await self.wrapped.read_content(content_id)

    async def read_text(self, content_id: str) -> str:
        return await self.wrapped.read_text(content_id)

    async def read_range(self, content_id: str, *, offset: int, length: int):
        return await self.wrapped.read_range(
            content_id,
            offset=offset,
            length=length,
        )

    async def search_text(self, content_id: str, *, query: str, limit: int = 10):
        return await self.wrapped.search_text(
            content_id,
            query=query,
            limit=limit,
        )
