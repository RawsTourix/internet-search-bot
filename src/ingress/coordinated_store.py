"""Durable exact draft joins and quiet-aware grouped commits."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from .grouping import FileSystemGroupedInputBatchStore, _OPEN_STATES
from .models import (
    ClientIngressEvent,
    InputAttachmentPart,
    InputBatchDraft,
    InputBatchDraftState,
    InputGroupingMode,
    utc_now,
)
from .store import IngressConflictError


class FileSystemCoordinatedInputBatchStore(FileSystemGroupedInputBatchStore):
    """Add exact draft joins without moving policy into transport handlers."""

    async def append_event_to_batch(
        self,
        input_batch_id: str,
        event: ClientIngressEvent,
    ) -> InputBatchDraft:
        return await asyncio.to_thread(
            self._append_event_to_batch_sync,
            input_batch_id,
            event,
        )

    def _append_event_to_batch_sync(
        self,
        input_batch_id: str,
        event: ClientIngressEvent,
    ) -> InputBatchDraft:
        with self._lock:
            draft = self._load_draft_sync(input_batch_id)
            if draft.state not in _OPEN_STATES:
                raise IngressConflictError(
                    "Input batch is no longer open for additional events"
                )
            return self._append_event_sync(draft, event)

    def _append_event_sync(
        self,
        draft: InputBatchDraft,
        event: ClientIngressEvent,
    ) -> InputBatchDraft:
        """Append by stable principal identity, not mutable display metadata."""

        if event.event_id in draft.source_event_ids:
            return draft
        if len(draft.source_event_ids) >= self.ingress_config.max_events_per_batch:
            raise IngressConflictError("Input batch event limit exceeded")
        if draft.client_type != event.client_type:
            raise IngressConflictError("Grouped input client type mismatch")
        if (
            draft.conversation != event.conversation
            or draft.sender.principal_id != event.sender.principal_id
        ):
            raise IngressConflictError("Grouped input authority mismatch")

        existing_part_ids = {item.part_id for item in draft.text_parts}
        existing_slot_ids = {item.slot_id for item in draft.attachment_parts}
        new_part_ids = {item.part_id for item in event.text_parts}
        new_slot_ids = {item.slot_id for item in event.attachment_slots}
        if existing_part_ids & new_part_ids:
            raise IngressConflictError("Grouped input text part ID collision")
        if existing_slot_ids & new_slot_ids:
            raise IngressConflictError("Grouped input attachment slot collision")
        if (
            len(draft.text_parts) + len(event.text_parts)
            > self.ingress_config.max_text_parts_per_batch
        ):
            raise IngressConflictError("Input batch text part limit exceeded")
        if (
            len(draft.attachment_parts) + len(event.attachment_slots)
            > self.ingress_config.max_attachments_per_batch
        ):
            raise IngressConflictError("Input batch attachment limit exceeded")

        now = utc_now()
        updated = draft.model_copy(update={
            "source_event_ids": [*draft.source_event_ids, event.event_id],
            "text_parts": [*draft.text_parts, *event.text_parts],
            "attachment_parts": [
                *draft.attachment_parts,
                *[
                    InputAttachmentPart(
                        slot_id=slot.slot_id,
                        original_filename=slot.original_filename,
                        declared_mime_type=slot.declared_mime_type,
                        declared_size_bytes=slot.declared_size_bytes,
                    )
                    for slot in event.attachment_slots
                ],
            ],
            "last_event_at": event.occurred_at,
            "updated_at": now,
            "state": InputBatchDraftState.COLLECTING,
        })
        updated = InputBatchDraft.model_validate(
            updated.model_dump(mode="python")
        )
        updated = self._apply_deadlines_sync(updated, reset_quiet=True)
        self._write_json(
            self.root / updated.input_batch_id / "draft.json",
            updated.model_dump(mode="json"),
        )
        self._write_json(
            self.event_index_dir / f"{event.event_id}.json",
            {
                "schema_version": 1,
                "event_id": event.event_id,
                "input_batch_id": updated.input_batch_id,
            },
        )
        return updated

    async def commit_batch(
        self,
        input_batch_id: str,
        *,
        session_id: str,
        reason: str,
    ):
        """Respect durable quiet deadlines before publishing a grouped batch.

        A transport debounce may request commit early. The store remains the
        source of truth and rechecks deadlines so a later text/file event can
        safely return the draft to collecting without a premature commit.
        """

        while True:
            draft = await self.get_draft(input_batch_id)
            if draft.session_id != session_id:
                raise IngressConflictError(
                    "Input batch belongs to another session"
                )
            now = datetime.now(timezone.utc)
            quiet_deadline = draft.quiet_deadline
            maximum_deadline = draft.maximum_deadline
            should_wait = (
                draft.grouping_mode == InputGroupingMode.MEDIA_GROUP
                and quiet_deadline is not None
                and now < quiet_deadline
                and (maximum_deadline is None or now < maximum_deadline)
            )
            if not should_wait:
                break
            quiet_remaining = (quiet_deadline - now).total_seconds()
            maximum_remaining = (
                (maximum_deadline - now).total_seconds()
                if maximum_deadline is not None
                else quiet_remaining
            )
            await asyncio.sleep(
                max(0.001, min(0.25, quiet_remaining, maximum_remaining))
            )

        return await super().commit_batch(
            input_batch_id,
            session_id=session_id,
            reason=reason,
        )
