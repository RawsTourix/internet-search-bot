"""Pure client session ID resolution shared by compatibility transports."""

from __future__ import annotations

from typing import Any, Mapping

from .models import ClientType


def resolve_message_session_id(
    *,
    client_type: ClientType,
    metadata: Mapping[str, Any] | None,
    user_id: str,
) -> str:
    values = dict(metadata or {})
    explicit = values.get("session_id")
    if explicit is not None:
        normalized = str(explicit).strip()
        if not normalized:
            raise ValueError("session_id must not be empty")
        if ":" in normalized:
            return normalized
        return f"{client_type.value}:session:{normalized}"

    chat_id = values.get("chat_id")
    if chat_id is not None:
        return f"{client_type.value}:chat:{chat_id}"

    normalized_user = str(user_id).strip()
    if not normalized_user:
        raise ValueError("user_id must not be empty")
    return f"{client_type.value}:user:{normalized_user}"
