from __future__ import annotations

import json
import time
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


AgentStatusName = Literal[
    "running",
    "waiting_user",
    "done",
    "error",
]

AgentActionName = Literal[
    "continue",
    "answer",
    "ask_user",
    "error",
]


class AgentAction(BaseModel):
    """
    JSON-ответ агента, когда он НЕ вызывает tool_call.

    Если модель вызывает инструмент через native tool calling,
    content может быть null, и AgentAction не нужен.
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["agent_action"] = "agent_action"

    status: AgentStatusName = Field(
        ...,
        description="Текущее состояние агента.",
    )

    action: AgentActionName = Field(
        ...,
        description="Что агент делает этим сообщением.",
    )

    agent_request: str | None = Field(
        default=None,
        description="Короткое пояснение для пользователя о текущем действии.",
    )

    final_answer: str | None = Field(
        default=None,
        description="Финальный ответ пользователю, если action='answer'.",
    )

    question_to_user: str | None = Field(
        default=None,
        description="Вопрос пользователю, если action='ask_user'.",
    )

    error_message: str | None = Field(
        default=None,
        description="Описание ошибки, если action='error'.",
    )


class ProgressEvent(BaseModel):
    """Событие прогресса для UI/Telegram/Web и trace."""

    model_config = ConfigDict(extra="forbid")

    type: Literal[
        "cycle_started",
        "cycle_resumed",
        "iteration_started",
        "agent_message",
        "tool_start",
        "tool_done",
        "tool_error",
        "llm_retry",
        "llm_error",
        "infrastructure_error",
        "context_warning",
        "context_compaction_started",
        "context_compaction_done",
        "large_result_saved",
        "final_processing_started",
        "waiting_user",
        "cycle_done",
        "cycle_error",
    ]

    message: str

    event_id: str = Field(default_factory=lambda: uuid4().hex)
    created_at: float = Field(default_factory=time.time)

    session_id: str | None = None
    cycle_id: str | None = None
    iteration: int | None = None

    tool_name: str | None = None
    target_tool_name: str | None = None
    server_name: str | None = None

    severity: Literal["info", "success", "warning", "error"] = "info"
    visibility: Literal["user", "debug", "internal"] = "user"

    data: dict[str, Any] | None = None


def dumps_json(data: Any) -> str:
    """Единая сериализация JSON для сообщений LLM."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def loads_json_object(text: str) -> dict[str, Any]:
    """Парсит только JSON-object."""
    data = json.loads(text)

    if not isinstance(data, dict):
        raise ValueError("Expected JSON object")

    return data
