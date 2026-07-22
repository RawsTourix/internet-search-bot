"""Deterministic routing from semantic client envelopes to durable grouping."""

from __future__ import annotations

from dataclasses import dataclass

from ..core.models import ClientType
from .models import ClientInputEnvelope, InputGroupingMode


@dataclass(frozen=True, slots=True)
class InputGroupingDecision:
    mode: InputGroupingMode
    key: str


def resolve_input_grouping(envelope: ClientInputEnvelope) -> InputGroupingDecision:
    """Choose grouping from authoritative semantic fields, never arbitrary paths.

    Web uploads and text-only messages are atomic. Telegram attachments are kept
    as grouped drafts so the Telegram client can explicitly seal one standalone
    file or debounce all members of a media group before starting the agent.
    """

    conversation = envelope.conversation.conversation_id
    thread = envelope.conversation.thread_id or "-"
    sender = envelope.sender.principal_id
    prefix = (
        f"{envelope.client_type.value}:{envelope.client_instance_id}:"
        f"{conversation}:{thread}:{sender}"
    )

    if (
        envelope.client_type == ClientType.TELEGRAM
        and envelope.attachment_slots
    ):
        if envelope.source_group_id:
            return InputGroupingDecision(
                mode=InputGroupingMode.MEDIA_GROUP,
                key=f"{prefix}:media-group:{envelope.source_group_id}",
            )
        return InputGroupingDecision(
            mode=InputGroupingMode.STANDALONE_ATTACHMENT,
            key=f"{prefix}:message:{envelope.source_message_id}",
        )

    return InputGroupingDecision(
        mode=InputGroupingMode.ATOMIC,
        key=f"{prefix}:event:{envelope.idempotency_key}",
    )
