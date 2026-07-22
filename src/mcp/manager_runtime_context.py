"""Task-local context shared by all manager-tool domains."""

from __future__ import annotations

from contextvars import ContextVar, Token

from .manager_context import ManagerToolContext


_CURRENT_MANAGER_CONTEXT: ContextVar[ManagerToolContext | None] = ContextVar(
    "manager_tool_context",
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
