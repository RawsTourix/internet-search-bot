"""Authoritative Telegram execution context and preflight receipt helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ...interaction.output_models import (
    ArtifactContentReceiptState,
    ArtifactOutputPart,
    OutputBatch,
    OutputDeliveryReceipt,
    OutputDeliveryReceiptState,
    OutputPartReceipt,
    OutputPartReceiptState,
)
from .output_plan_executor import TelegramExecutionContext


def build_telegram_execution_context(
    batch: OutputBatch,
    *,
    bot: Any,
    gateway: Any,
    status_message_id: int | None = None,
) -> TelegramExecutionContext:
    """Resolve transport addressing only from the immutable OutputBatch."""

    route = batch.response_route
    if route.route_type.strip().lower() != "telegram":
        raise ValueError("non-Telegram response route")
    chat_id = _required_int(route.conversation_id, "conversation_id")
    thread_id = _optional_int(route.thread_id, "thread_id")
    anchor_id = (
        batch.response_anchor.client_message_id
        if batch.response_anchor is not None
        else None
    )
    reply_id = _optional_int(
        anchor_id or route.reply_to_message_id,
        "reply_to_message_id",
    )
    return TelegramExecutionContext(
        bot=bot,
        gateway=gateway,
        session_id=batch.session_id,
        chat_id=chat_id,
        message_thread_id=thread_id,
        reply_to_message_id=reply_id,
        status_message_id=status_message_id,
    )


def build_preflight_failure_receipt(
    batch: OutputBatch,
    *,
    attempt_id: str,
    error_category: str,
) -> OutputDeliveryReceipt:
    """Close a claimed batch when no Telegram transport attempt can start."""

    now = datetime.now(timezone.utc)
    receipts = tuple(
        OutputPartReceipt(
            part_id=part.part_id,
            index=part.index,
            state=OutputPartReceiptState.FAILED,
            required=part.required,
            delivery_id=getattr(part, "delivery_id", None),
            artifact_content_state=(
                ArtifactContentReceiptState.NOT_DELIVERED
                if isinstance(part, ArtifactOutputPart)
                else None
            ),
            error_category=error_category,
        )
        for part in batch.parts
    )
    return OutputDeliveryReceipt(
        output_batch_id=batch.output_batch_id,
        attempt_id=attempt_id,
        state=OutputDeliveryReceiptState.FAILED,
        part_receipts=receipts,
        started_at=now,
        completed_at=now,
    )


def _required_int(value: Any, field_name: str) -> int:
    parsed = _optional_int(value, field_name)
    if parsed is None:
        raise ValueError(f"{field_name} must not be empty")
    return parsed


def _optional_int(value: Any, field_name: str) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be an integer ID")
    try:
        return int(str(value).strip())
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be an integer ID") from error
