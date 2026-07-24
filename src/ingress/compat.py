"""Compatibility normalization from legacy message models to semantic ingress."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..core.models import UnifiedMessage
from .models import (
    ClientConversationRef,
    ClientInputEnvelope,
    ClientResponseRoute,
    ClientSenderRef,
    IngressTextPart,
)


def _aware_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _thread_from_metadata(metadata: dict[str, Any]) -> str | None:
    value = metadata.get("thread_id") or metadata.get("message_thread_id")
    if value is not None and str(value).strip():
        return str(value).strip()

    session_id = str(metadata.get("session_id") or "")
    marker = ":thread:"
    if marker in session_id:
        candidate = session_id.rsplit(marker, 1)[-1].strip()
        return candidate or None
    return None


def legacy_message_to_input_envelope(
    message: UnifiedMessage,
    *,
    client_instance_id: str | None = None,
) -> ClientInputEnvelope:
    """Convert a server-owned legacy message into the shared ingress contract.

    This is a compatibility boundary only. New transports should construct a
    ``ClientInputEnvelope`` directly and avoid the legacy message model.
    """

    metadata = dict(message.metadata or {})
    instance_id = str(
        client_instance_id
        or metadata.get("client_instance_id")
        or metadata.get("bot_instance_id")
        or f"{message.client_type.value}-legacy"
    ).strip()
    if not instance_id:
        raise ValueError("client_instance_id must not be empty")

    conversation_id = str(
        metadata.get("conversation_id")
        or metadata.get("chat_id")
        or metadata.get("session_id")
        or message.user_id
    ).strip()
    if not conversation_id:
        raise ValueError("legacy message has no conversation identity")

    source_message_id = str(
        metadata.get("message_id")
        or metadata.get("source_message_id")
        or message.id
    ).strip()
    if not source_message_id:
        raise ValueError("legacy message has no source message identity")

    text = str(message.content or "").strip()
    if not text:
        raise ValueError("legacy text message must not be empty")

    thread_id = _thread_from_metadata(metadata)
    reply_to_message_id = metadata.get("reply_to_message_id")
    if reply_to_message_id is not None:
        reply_to_message_id = str(reply_to_message_id).strip() or None

    compatibility_metadata = dict(metadata)
    compatibility_metadata.update({
        "legacy_message_id": message.id,
        "legacy_message_type": message.message_type.value,
        "legacy_compatibility_wrapper": True,
    })
    if message.command:
        compatibility_metadata["legacy_command"] = message.command
    if message.arguments:
        compatibility_metadata["legacy_arguments"] = list(message.arguments)

    return ClientInputEnvelope(
        idempotency_key=(
            f"legacy:{message.client_type.value}:{instance_id}:"
            f"{conversation_id}:{thread_id or '-'}:{source_message_id}"
        ),
        client_type=message.client_type,
        client_instance_id=instance_id,
        conversation=ClientConversationRef(
            conversation_id=conversation_id,
            thread_id=thread_id,
        ),
        sender=ClientSenderRef(
            principal_id=str(message.user_id),
            display_name=message.user_name,
        ),
        source_update_id=str(
            metadata.get("update_id")
            or metadata.get("source_update_id")
            or message.id
        ),
        source_message_id=source_message_id,
        source_group_id=None,
        reply_to_message_id=reply_to_message_id,
        occurred_at=_aware_timestamp(message.timestamp),
        text_parts=[IngressTextPart(
            part_id=f"message-{source_message_id}",
            kind="message_text",
            text=text,
            attachment_slot_ids=[],
        )],
        attachment_slots=[],
        locale=(str(metadata.get("locale")).strip() if metadata.get("locale") else None),
        response_route=ClientResponseRoute(
            route_type=message.client_type.value,
            conversation_id=conversation_id,
            thread_id=thread_id,
            reply_to_message_id=source_message_id,
            metadata=metadata,
        ),
        metadata=compatibility_metadata,
    )
