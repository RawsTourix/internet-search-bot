"""Coordinator for structured, single-handle InputBatch acknowledgements."""

from __future__ import annotations

import secrets
import logging
from datetime import datetime, timezone

from ..localization.models import LocalizationMessage
from .config import InputPresentationConfig
from .presentation import (
    InputPresentationEvent,
    PresentationAckPolicy,
    PresentationState,
    PublicPresentationRef,
)
from .presentation_store import FileSystemInputPresentationStore


logger = logging.getLogger("Interaction.Presentation")


class InputPresentationCoordinator:
    def __init__(
        self,
        store: FileSystemInputPresentationStore,
        *,
        config: InputPresentationConfig | None = None,
    ) -> None:
        self.store = store
        self.config = config or InputPresentationConfig()

    async def present(
        self,
        *,
        input_batch_id: str,
        client_binding_id: str,
        locale: str,
        state: str,
        file_count: int,
        text_part_count: int,
        response_anchor,
    ) -> tuple[PresentationAckPolicy, InputPresentationEvent, PublicPresentationRef]:
        key = {
            "collecting": "input_batch.collecting",
            "committed": "input_batch.committed",
            "failed": "input_batch.failed",
        }.get(state, "input_batch.updated")
        params = {
            "file_count": file_count,
            "text_part_count": text_part_count,
        }
        message = LocalizationMessage(
            message_key=key,
            params=params,
            severity="error" if state == "failed" else "info",
        )
        token = secrets.token_urlsafe(32)
        stored, created = await self.store.reserve(
            input_batch_id=input_batch_id,
            client_binding_id=client_binding_id,
            token=token,
            message=message,
            locale=locale,
            file_count=file_count,
            text_part_count=text_part_count,
            response_anchor=response_anchor,
        )
        if not created:
            previous_updated_at = stored.updated_at
            previous_update_count = stored.update_count
            stored = await self.store.update(
                stored.presentation_id,
                message=message,
                file_count=file_count,
                text_part_count=text_part_count,
                response_anchor=response_anchor,
            )
        logger.info(
            "%s input_batch_id=%s presentation_id=%s file_count=%s "
            "text_part_count=%s",
            (
                "input_batch_presentation_created"
                if created
                else "input_batch_presentation_updated"
            ),
            input_batch_id,
            stored.presentation_id,
            file_count,
            text_part_count,
        )
        if response_anchor is not None:
            logger.info(
                "input_batch_response_anchor_updated input_batch_id=%s "
                "anchor_id=%s kind=%s",
                input_batch_id,
                response_anchor.anchor_id,
                response_anchor.kind.value,
            )
        if state in {"committed", "failed"} and stored.state not in {
            PresentationState.CLOSED,
            PresentationState.FAILED,
            PresentationState.EXPIRED,
        }:
            stored = await self.store.close(
                stored.presentation_id,
                state=(
                    PresentationState.CLOSED
                    if state == "committed"
                    else PresentationState.FAILED
                ),
                error_code=None if state == "committed" else "input_batch_failed",
            )
        policy = (
            PresentationAckPolicy.CREATE
            if created
            else (
                PresentationAckPolicy.SILENT
                if (
                    not stored.client_message_id
                    or previous_update_count >= self.config.max_updates_per_batch
                )
                else (
                    PresentationAckPolicy.THROTTLED_UPDATE
                    if (
                        datetime.now(timezone.utc) - previous_updated_at
                    ).total_seconds() < self.config.update_throttle_seconds
                    else PresentationAckPolicy.UPDATE_EXISTING
                )
            )
        )
        event = InputPresentationEvent(
            message_key=message.message_key,
            severity="error" if state == "failed" else "info",
            params=params,
            locale=locale,
        )
        return (
            policy,
            event,
            PublicPresentationRef(
                presentation_id=stored.presentation_id,
                presentation_token=token if created else None,
                client_message_id=stored.client_message_id,
                state=stored.state,
            ),
        )
