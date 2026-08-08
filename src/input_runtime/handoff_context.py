"""Task-local runtime handoff identity for the exact admitted invocation.

The context is runtime-owned: transports and the LLM never supply these values.
It only carries the already-durable RuntimeHandoff identity across the existing
Api -> MCPClient call stack so IR-7 can bind finalization to that exact invocation.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RuntimeHandoffExecutionContext:
    admission_id: str
    session_id: str
    cycle_id: str
    generation: int
    handoff_token: str


_current_runtime_handoff: ContextVar[RuntimeHandoffExecutionContext | None] = (
    ContextVar("input_runtime_current_handoff", default=None)
)


def activate_runtime_handoff_context(
    *,
    admission_id: str,
    session_id: str,
    cycle_id: str,
    generation: int,
    handoff_token: str,
) -> RuntimeHandoffExecutionContext:
    values = {
        "admission_id": admission_id,
        "session_id": session_id,
        "cycle_id": cycle_id,
        "handoff_token": handoff_token,
    }
    normalized = {key: str(value).strip() for key, value in values.items()}
    if not all(normalized.values()):
        raise ValueError("runtime handoff execution identity must be complete")
    if generation < 0:
        raise ValueError("runtime handoff generation must be non-negative")
    context = RuntimeHandoffExecutionContext(
        admission_id=normalized["admission_id"],
        session_id=normalized["session_id"],
        cycle_id=normalized["cycle_id"],
        generation=int(generation),
        handoff_token=normalized["handoff_token"],
    )
    _current_runtime_handoff.set(context)
    return context


def get_runtime_handoff_context() -> RuntimeHandoffExecutionContext | None:
    return _current_runtime_handoff.get()


def clear_runtime_handoff_context_if_matches(
    *,
    admission_id: str,
    handoff_token: str,
) -> None:
    current = _current_runtime_handoff.get()
    if current is None:
        return
    if (
        current.admission_id == str(admission_id).strip()
        and current.handoff_token == str(handoff_token).strip()
    ):
        _current_runtime_handoff.set(None)


def clear_runtime_handoff_context_for_tests() -> None:
    _current_runtime_handoff.set(None)
