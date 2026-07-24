"""Shared logical-input grouping above the transport-neutral ingress service."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping

from .models import ClientInputEnvelope, InputGroupingMode, InputSubmissionResult
from .routing import resolve_input_grouping
from .service import ArtifactIngressService


logger = logging.getLogger("API.Ingress.Grouping")


class UnifiedArtifactIngressService(ArtifactIngressService):
    """Apply one authoritative grouping policy for every transport entrypoint.

    Callers may still pass a grouping hint for compatibility, but the durable
    store and currently open drafts determine the final logical InputBatch.
    This class intentionally stops before cycle admission; ``CycleInbox`` and
    active-cycle continuation belong to ``v0.4-input-runtime``.
    """

    async def submit_atomic(
        self,
        envelope: ClientInputEnvelope,
        *,
        session_id: str,
        upload_streams: Mapping[str, AsyncIterator[bytes]] | None = None,
        grouping_mode: InputGroupingMode = InputGroupingMode.ATOMIC,
        grouping_key: str | None = None,
    ) -> InputSubmissionResult:
        list_open = getattr(self.batch_store, "list_open_drafts", None)
        open_drafts = (
            await list_open(session_id=session_id)
            if list_open is not None
            else []
        )
        decision = resolve_input_grouping(
            envelope,
            open_drafts=open_drafts,
        )
        if (
            grouping_mode != decision.mode
            or (grouping_key is not None and grouping_key != decision.key)
        ):
            logger.info(
                "ingress_grouping_hint_overridden session_id=%s "
                "source_message_id=%s hinted_mode=%s resolved_mode=%s "
                "joined_input_batch_id=%s",
                session_id,
                envelope.source_message_id,
                grouping_mode.value,
                decision.mode.value,
                decision.joined_input_batch_id,
            )
        else:
            logger.info(
                "ingress_grouping_resolved session_id=%s source_message_id=%s "
                "mode=%s joined_input_batch_id=%s",
                session_id,
                envelope.source_message_id,
                decision.mode.value,
                decision.joined_input_batch_id,
            )
        return await super().submit_atomic(
            envelope,
            session_id=session_id,
            upload_streams=upload_streams,
            grouping_mode=decision.mode,
            grouping_key=decision.key,
        )
