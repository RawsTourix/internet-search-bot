"""JSON-safe projections of AgentResult for transport responses."""

from __future__ import annotations

from typing import Any


def _json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raw = getattr(value, "value", value)
    return raw


def agent_result_metadata(agent_result: Any) -> dict[str, Any]:
    return {
        "agent_status": _json_value(agent_result.status),
        "session_id": agent_result.session_id,
        "cycle_id": getattr(agent_result, "cycle_id", None),
        "iterations": agent_result.iterations,
        "tools_used": list(agent_result.tools_used),
        "error": agent_result.error,
        "error_kind": agent_result.error_kind,
        "can_resume": bool(agent_result.can_resume),
        "progress_events": [
            _json_value(item) for item in agent_result.progress_events
        ],
        "artifacts": [
            _json_value(item) for item in agent_result.artifacts
        ],
        "semantic_outputs": [
            _json_value(item)
            for item in getattr(agent_result, "semantic_outputs", [])
        ],
        "output_batch": _json_value(
            getattr(agent_result, "output_batch", None)
        ),
    }
