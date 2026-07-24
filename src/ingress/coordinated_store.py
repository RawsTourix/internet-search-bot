"""Durable exact draft joins and quiet-aware grouped commits."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from .grouping import FileSystemGroupedInputBatchStore, _OPEN_STATES
from .models import ClientIngressEvent, InputBatchDraft, InputGroupingMode
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
