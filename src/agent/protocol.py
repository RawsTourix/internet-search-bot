from __future__ import annotations

import json
from typing import Any, Literal

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
    """Событие прогресса для UI/Telegram/Web."""

    model_config = ConfigDict(extra="forbid")

    type: Literal[
        "agent_message",
        "tool_start",
        "tool_done",
        "tool_error",
    ]

    message: str
    tool_name: str | None = None
    server_name: str | None = None
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