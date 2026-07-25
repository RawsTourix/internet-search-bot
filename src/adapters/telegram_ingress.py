"""Pure Telegram-to-ingress normalization without runtime side effects."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..core.models import ClientType
from ..interaction.parts import InputPart
from ..ingress import (
    ClientAttachmentLocator,
    ClientConversationRef,
    ClientInputEnvelope,
    ClientResponseRoute,
    ClientSenderRef,
    IngressAttachmentSlot,
    IngressTextPart,
    InputAdmissionMode,
)


def build_telegram_input_envelope(
    *,
    bot_instance_id: str,
    update_id: str,
    chat_id: str,
    user_id: str,
    message_id: str,
    attachments: list[dict[str, Any]],
    semantic_parts: list[InputPart] | None = None,
    user_name: str | None = None,
    caption: str | None = None,
    media_group_id: str | None = None,
    message_thread_id: str | int | None = None,
    reply_to_message_id: str | None = None,
    occurred_at: datetime | None = None,
    locale: str | None = None,
    response_metadata: dict[str, Any] | None = None,
    admission_mode: InputAdmissionMode = InputAdmissionMode.AUTO,
) -> ClientInputEnvelope:
    """Build one semantic envelope using opaque Telegram ``file_id`` locators."""

    normalized_bot = str(bot_instance_id).strip()
    normalized_update = str(update_id).strip()
    normalized_message = str(message_id).strip()
    normalized_chat = str(chat_id).strip()
    normalized_user = str(user_id).strip()
    if not all((
        normalized_bot,
        normalized_update,
        normalized_message,
        normalized_chat,
        normalized_user,
    )):
        raise ValueError("Telegram ingress authority fields must not be empty")

    slots: list[IngressAttachmentSlot] = []
    for index, attachment in enumerate(attachments, start=1):
        file_id = str(attachment.get("file_id") or "").strip()
        if not file_id:
            raise ValueError("Telegram attachment requires file_id")
        slot_id = f"slot_{normalized_message}-{index}"
        slots.append(IngressAttachmentSlot(
            slot_id=slot_id,
            media_kind=str(attachment.get("media_kind") or "document"),
            original_filename=attachment.get("filename"),
            declared_mime_type=attachment.get("mime_type"),
            declared_size_bytes=attachment.get("size_bytes"),
            transport_locator=ClientAttachmentLocator(
                provider="telegram",
                locator=file_id,
            ),
            metadata={
                "telegram_file_unique_id": attachment.get("file_unique_id"),
                "telegram_media_group_id": media_group_id,
            },
        ))

    text_parts: list[IngressTextPart] = []
    normalized_caption = (caption or "").strip()
    if normalized_caption:
        text_parts.append(IngressTextPart(
            part_id=f"caption-{normalized_message}",
            kind="caption",
            text=normalized_caption,
            attachment_slot_ids=[item.slot_id for item in slots],
        ))

    timestamp = occurred_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)

    return ClientInputEnvelope(
        idempotency_key=(
            f"telegram:{normalized_bot}:update:{normalized_update}:"
            f"message:{normalized_message}"
        ),
        client_type=ClientType.TELEGRAM,
        client_instance_id=normalized_bot,
        conversation=ClientConversationRef(
            conversation_id=normalized_chat,
            thread_id=(
                str(message_thread_id)
                if message_thread_id is not None
                else None
            ),
        ),
        sender=ClientSenderRef(
            principal_id=normalized_user,
            display_name=user_name,
        ),
        source_update_id=normalized_update,
        source_message_id=normalized_message,
        source_group_id=(
            str(media_group_id) if media_group_id is not None else None
        ),
        reply_to_message_id=reply_to_message_id,
        occurred_at=timestamp,
        text_parts=text_parts,
        attachment_slots=slots,
        semantic_parts=list(semantic_parts or []),
        locale=locale,
        admission_mode=admission_mode,
        response_route=ClientResponseRoute(
            route_type="telegram",
            conversation_id=normalized_chat,
            thread_id=(
                str(message_thread_id)
                if message_thread_id is not None
                else None
            ),
            reply_to_message_id=normalized_message,
            metadata=dict(response_metadata or {}),
        ),
        metadata={
            "telegram_update_id": normalized_update,
            "telegram_media_group_id": media_group_id,
        },
    )
