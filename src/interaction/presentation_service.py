"""Coordinator for structured, generational InputBatch acknowledgements."""

from __future__ import annotations

import logging
import secrets
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

        previous_updated_at = stored.updated_at
        previous_update_count = stored.update_count
        relocation_reserved = False
        previous_message_id: str | None = None
        if not created:
            if self._should_relocate(
                stored,
                state=state,
                response_anchor=response_anchor,
            ):
                previous_message_id = stored.client_message_id
                stored = await self.store.reserve_relocation(
                    stored.presentation_id,
                    token=token,
                    expected_generation=stored.presentation_generation,
                    message=message,
                    file_count=file_count,
                    text_part_count=text_part_count,
                    response_anchor=response_anchor,
                )
                relocation_reserved = True
                logger.info(
                    "input_batch_presentation_relocation_reserved "
                    "input_batch_id=%s presentation_id=%s generation=%s "
                    "anchor_message_id=%s",
                    input_batch_id,
                    stored.presentation_id,
                    stored.pending_relocation_generation,
                    response_anchor.client_message_id,
                )
            else:
                stored = await self.store.update(
                    stored.presentation_id,
                    message=message,
                    file_count=file_count,
                    text_part_count=text_part_count,
                    response_anchor=response_anchor,
                )

        logger.info(
            "%s input_batch_id=%s presentation_id=%s file_count=%s "
            "text_part_count=%s generation=%s",
            (
                "input_batch_presentation_created"
                if created
                else "input_batch_presentation_updated"
            ),
            input_batch_id,
            stored.presentation_id,
            file_count,
            text_part_count,
            stored.presentation_generation,
        )
        if response_anchor is not None:
            logger.info(
                "input_batch_response_anchor_updated input_batch_id=%s "
                "anchor_id=%s kind=%s",
                input_batch_id,
                response_anchor.anchor_id,
                response_anchor.kind.value,
            )

        if state in {"committed", "failed"}:
            terminal = (
                PresentationState.CLOSED
                if state == "committed"
                else PresentationState.FAILED
            )
            if stored.state == PresentationState.RESERVED:
                stored = await self.store.defer_terminal(
                    stored.presentation_id,
                    state=terminal,
                    error_code=(
                        None if state == "committed" else "input_batch_failed"
                    ),
                )
            elif stored.state == PresentationState.BOUND:
                stored = await self.store.close(
                    stored.presentation_id,
                    state=terminal,
                    error_code=(
                        None if state == "committed" else "input_batch_failed"
                    ),
                )

        if created:
            policy = PresentationAckPolicy.CREATE
        elif relocation_reserved:
            policy = PresentationAckPolicy.RELOCATE
        elif stored.pending_relocation_generation is not None:
            # A client already owns the create/bind attempt for the next
            # generation. Do not edit the current handle or create another one.
            policy = PresentationAckPolicy.SILENT
        elif (
            not stored.client_message_id
            or previous_update_count >= self.config.max_updates_per_batch
        ):
            policy = PresentationAckPolicy.SILENT
        elif (
            datetime.now(timezone.utc) - previous_updated_at
        ).total_seconds() < self.config.update_throttle_seconds:
            policy = PresentationAckPolicy.THROTTLED_UPDATE
        else:
            policy = PresentationAckPolicy.UPDATE_EXISTING

        event = InputPresentationEvent(
            message_key=message.message_key,
            severity="error" if state == "failed" else "info",
            params=params,
            locale=locale,
        )
        public_token = token if created or relocation_reserved else None
        return (
            policy,
            event,
            PublicPresentationRef(
                presentation_id=stored.presentation_id,
                presentation_token=public_token,
                client_message_id=stored.client_message_id,
                active_client_message_id=stored.client_message_id,
                state=stored.state,
                presentation_generation=stored.presentation_generation,
                relocation_generation=stored.pending_relocation_generation,
                previous_client_message_id=previous_message_id,
            ),
        )

    async def finalize_batch(
        self,
        *,
        input_batch_id: str,
        state: str,
        file_count: int,
        text_part_count: int,
        response_anchor,
    ) -> tuple[
        PresentationAckPolicy,
        InputPresentationEvent,
        PublicPresentationRef,
    ] | None:
        """Apply a grouped commit to the already reserved presentation."""
        records = await self.store.list_for_input_batch(input_batch_id)
        active = [
            item
            for item in records
            if item.state in {
                PresentationState.RESERVED,
                PresentationState.BOUND,
            }
        ]
        if not active:
            return None
        if len(active) != 1:
            raise RuntimeError(
                "input batch has multiple active presentation handles"
            )
        record = active[0]
        return await self.present(
            input_batch_id=input_batch_id,
            client_binding_id=record.client_binding_id,
            locale=record.locale,
            state=state,
            file_count=file_count,
            text_part_count=text_part_count,
            response_anchor=response_anchor,
        )

    @staticmethod
    def _should_relocate(stored, *, state: str, response_anchor) -> bool:
        if (
            state != "collecting"
            or stored.state != PresentationState.BOUND
            or stored.pending_relocation_generation is not None
            or stored.client_message_id is None
            or response_anchor is None
        ):
            return False
        # v0.4 enables client-order relocation for the Telegram adapter. Other
        # transports keep their current handle until they expose an equivalent
        # ordered-message capability contract.
        if not stored.client_binding_id.startswith("telegram:"):
            return False
        try:
            active_order = int(stored.client_message_id)
            anchor_order = int(response_anchor.client_message_id)
        except (TypeError, ValueError):
            return False
        return anchor_order > active_order
