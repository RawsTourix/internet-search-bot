"""Deterministic routing from semantic client envelopes to durable grouping."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ..core.models import ClientType
from .models import (
    ClientInputEnvelope,
    InputAdmissionMode,
    InputBatchDraft,
    InputBatchDraftState,
    InputGroupingMode,
)


_OPEN_DRAFT_STATES = {
    InputBatchDraftState.COLLECTING,
    InputBatchDraftState.SEALING,
    InputBatchDraftState.INGESTING,
    InputBatchDraftState.READY_TO_COMMIT,
}
_ATTACHMENT_GROUPING_MODES = {
    InputGroupingMode.MEDIA_GROUP,
    InputGroupingMode.STANDALONE_ATTACHMENT,
}


class InputGroupingAmbiguityError(ValueError):
    """A transport event cannot be joined to one authoritative open draft."""


@dataclass(frozen=True, slots=True)
class InputGroupingDecision:
    mode: InputGroupingMode
    key: str
    joined_input_batch_id: str | None = None

    @property
    def joins_existing_draft(self) -> bool:
        return self.joined_input_batch_id is not None


def _scope_prefix(envelope: ClientInputEnvelope) -> str:
    conversation = envelope.conversation.conversation_id
    thread = envelope.conversation.thread_id or "-"
    sender = envelope.sender.principal_id
    return (
        f"{envelope.client_type.value}:{envelope.client_instance_id}:"
        f"{conversation}:{thread}:{sender}"
    )


def _is_control_boundary(envelope: ClientInputEnvelope) -> bool:
    if envelope.admission_mode == InputAdmissionMode.NEW_CYCLE:
        return True
    metadata = envelope.metadata or {}
    return bool(
        metadata.get("is_control_event")
        or metadata.get("legacy_message_type") == "command"
    )


def _compatible_open_attachment_drafts(
    envelope: ClientInputEnvelope,
    open_drafts: Sequence[InputBatchDraft],
) -> list[InputBatchDraft]:
    result: list[InputBatchDraft] = []
    for draft in open_drafts:
        if draft.state not in _OPEN_DRAFT_STATES:
            continue
        if draft.grouping_mode not in _ATTACHMENT_GROUPING_MODES:
            continue
        if not draft.attachment_parts:
            continue
        if draft.client_type != envelope.client_type:
            continue
        if draft.conversation != envelope.conversation:
            continue
        if draft.sender.principal_id != envelope.sender.principal_id:
            continue
        result.append(draft)
    return result


def resolve_input_grouping(
    envelope: ClientInputEnvelope,
    *,
    open_drafts: Sequence[InputBatchDraft] = (),
) -> InputGroupingDecision:
    """Resolve one transport event into the shared logical-input grouping.

    Client-specific hints such as Telegram ``media_group_id`` are interpreted
    here, while draft mutation remains the responsibility of the common ingress
    store. Text-only events may join exactly one open attachment draft with the
    same authoritative conversation/thread/sender scope. Ambiguous joins are
    rejected instead of guessing.
    """

    prefix = _scope_prefix(envelope)

    if envelope.client_type == ClientType.TELEGRAM and envelope.attachment_slots:
        if envelope.source_group_id:
            return InputGroupingDecision(
                mode=InputGroupingMode.MEDIA_GROUP,
                key=f"{prefix}:media-group:{envelope.source_group_id}",
            )
        return InputGroupingDecision(
            mode=InputGroupingMode.STANDALONE_ATTACHMENT,
            key=f"{prefix}:message:{envelope.source_message_id}",
        )

    if (
        envelope.text_parts
        and not envelope.attachment_slots
        and not _is_control_boundary(envelope)
    ):
        candidates = _compatible_open_attachment_drafts(envelope, open_drafts)
        if len(candidates) == 1:
            draft = candidates[0]
            return InputGroupingDecision(
                mode=draft.grouping_mode,
                key=draft.grouping_key,
                joined_input_batch_id=draft.input_batch_id,
            )
        if len(candidates) > 1:
            raise InputGroupingAmbiguityError(
                "Text input matches multiple open attachment drafts; reply to "
                "the intended batch or commit/cancel one draft first."
            )

    return InputGroupingDecision(
        mode=InputGroupingMode.ATOMIC,
        key=f"{prefix}:event:{envelope.idempotency_key}",
    )
