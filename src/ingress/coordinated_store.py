"""Durable exact draft joins and quiet-aware grouped commits."""

from __future__ import annotations

import asyncio
from collections import Counter
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
from ..interaction.anchors import ResponseAnchorSelector
from .semantic_limits import (
    SemanticInputLimitError,
    validate_semantic_parts,
)


class FileSystemCoordinatedInputBatchStore(FileSystemGroupedInputBatchStore):
    """Add exact draft joins without moving policy into transport handlers."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._active_ingress_reservations: Counter[
            tuple[str, str, str, str]
        ] = Counter()

    def begin_ingress_reservation(
        self,
        scope: tuple[str, str, str, str],
    ) -> None:
        with self._lock:
            self._active_ingress_reservations[scope] += 1

    def end_ingress_reservation(
        self,
        scope: tuple[str, str, str, str],
    ) -> None:
        with self._lock:
            count = self._active_ingress_reservations.get(scope, 0)
            if count <= 1:
                self._active_ingress_reservations.pop(scope, None)
            else:
                self._active_ingress_reservations[scope] = count - 1

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
        if (
            draft.capability_snapshot is not None
            and event.capability_snapshot is not None
            and draft.capability_snapshot.capability_snapshot_id
            != event.capability_snapshot.capability_snapshot_id
        ):
            raise IngressConflictError("Grouped input capability binding mismatch")

        existing_part_ids = {item.part_id for item in draft.text_parts}
        existing_slot_ids = {item.slot_id for item in draft.attachment_parts}
        new_part_ids = {item.part_id for item in event.text_parts}
        new_slot_ids = {item.slot_id for item in event.attachment_slots}
        if existing_part_ids & new_part_ids:
            raise IngressConflictError("Grouped input text part ID collision")
        if existing_slot_ids & new_slot_ids:
            raise IngressConflictError("Grouped input attachment slot collision")
        existing_semantic_ids = {item.part_id for item in draft.semantic_parts}
        new_semantic_ids = {item.part_id for item in event.semantic_parts}
        if existing_semantic_ids & new_semantic_ids:
            raise IngressConflictError("Grouped input semantic part ID collision")
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
        try:
            validate_semantic_parts(
                [*draft.semantic_parts, *event.semantic_parts],
                self.ingress_config,
            )
        except SemanticInputLimitError as error:
            raise IngressConflictError(str(error)) from error

        now = utc_now()
        response_anchor = draft.response_anchor
        selector = ResponseAnchorSelector()
        for candidate in event.response_anchor_candidates:
            response_anchor = selector.select_with_current(
                response_anchor,
                candidate,
                selected_at=now,
            )
        updated = draft.model_copy(update={
            "source_event_ids": [*draft.source_event_ids, event.event_id],
            "text_parts": [*draft.text_parts, *event.text_parts],
            "semantic_parts": [*draft.semantic_parts, *event.semantic_parts],
            "reply_contexts": [
                *draft.reply_contexts,
                *(
                    [event.reply_context]
                    if event.reply_context is not None
                    else []
                ),
            ],
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
            "response_anchor": response_anchor,
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
            committed, delay = await asyncio.to_thread(
                self._commit_when_quiet_sync,
                input_batch_id,
                session_id,
                reason,
            )
            if committed is not None:
                return committed
            await asyncio.sleep(max(0.001, min(0.25, delay)))

    def _commit_when_quiet_sync(
        self,
        input_batch_id: str,
        session_id: str,
        reason: str,
    ):
        """Check the deadline and commit under the same lock as exact joins."""
        with self._lock:
            draft = self._load_draft_sync(input_batch_id)
            if draft.session_id != session_id:
                raise IngressConflictError(
                    "Input batch belongs to another session"
                )
            now = datetime.now(timezone.utc)
            maximum_is_open = (
                draft.maximum_deadline is None
                or now < draft.maximum_deadline
            )
            instance_id = (
                draft.capability_snapshot.client_instance_id
                if draft.capability_snapshot is not None
                else ""
            )
            reservation_scope = (
                draft.session_id,
                draft.client_type.value,
                instance_id,
                draft.sender.principal_id,
            )
            if (
                maximum_is_open
                and self._active_ingress_reservations.get(
                    reservation_scope,
                    0,
                )
                > 0
            ):
                return None, 0.01
            if (
                draft.grouping_mode == InputGroupingMode.MEDIA_GROUP
                and draft.quiet_deadline is not None
                and now < draft.quiet_deadline
                and (
                    maximum_is_open
                )
            ):
                quiet_remaining = (
                    draft.quiet_deadline - now
                ).total_seconds()
                maximum_remaining = (
                    (draft.maximum_deadline - now).total_seconds()
                    if draft.maximum_deadline is not None
                    else quiet_remaining
                )
                return None, min(quiet_remaining, maximum_remaining)
            return self._commit_grouped_sync(
                input_batch_id,
                session_id,
                reason,
            ), 0.0
