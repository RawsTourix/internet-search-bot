"""Failure recovery around durable logical-input submission."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping

from ..artifacts.errors import ArtifactIntegrityError, ArtifactStorageError
from .models import (
    ClientInputEnvelope,
    InputGroupingMode,
    InputSubmissionResult,
)
from .unified_service import UnifiedArtifactIngressService


logger = logging.getLogger("API.Ingress.Recovery")


class ResilientUnifiedArtifactIngressService(UnifiedArtifactIngressService):
    """Ensure a reserved draft cannot survive an infrastructure failure as open."""

    async def submit_atomic(
        self,
        envelope: ClientInputEnvelope,
        *,
        session_id: str,
        upload_streams: Mapping[str, AsyncIterator[bytes]] | None = None,
        grouping_mode: InputGroupingMode = InputGroupingMode.ATOMIC,
        grouping_key: str | None = None,
    ) -> InputSubmissionResult:
        try:
            return await super().submit_atomic(
                envelope,
                session_id=session_id,
                upload_streams=upload_streams,
                grouping_mode=grouping_mode,
                grouping_key=grouping_key,
            )
        except (ArtifactStorageError, ArtifactIntegrityError) as error:
            error_code = (
                "artifact_ingress_integrity_failed"
                if isinstance(error, ArtifactIntegrityError)
                else "artifact_ingress_storage_failed"
            )
            try:
                capability_snapshot, resolved_locale = (
                    await self._resolve_interaction(envelope)
                )
                event, _ = await self.event_store.save_if_absent(
                    envelope,
                    capability_snapshot=capability_snapshot,
                    resolved_locale=resolved_locale,
                )
                draft, committed = await self.batch_store.find_by_event(
                    event.event_id
                )
                if draft is not None and committed is None:
                    slot_id = (
                        envelope.attachment_slots[0].slot_id
                        if len(envelope.attachment_slots) == 1
                        else None
                    )
                    failed = await self.batch_store.fail(
                        draft.input_batch_id,
                        code=error_code,
                        slot_id=slot_id,
                    )
                    result = InputSubmissionResult(
                        event_id=event.event_id,
                        input_batch_id=failed.input_batch_id,
                        state="failed",
                        duplicate=False,
                        error_code=error_code,
                    )
                    try:
                        await self._decorate_result(result, envelope=envelope)
                    except Exception:
                        logger.exception(
                            "ingress_failure_presentation_finalize_failed "
                            "input_batch_id=%s event_id=%s",
                            failed.input_batch_id,
                            event.event_id,
                        )
                    logger.warning(
                        "ingress_reserved_draft_failed input_batch_id=%s "
                        "event_id=%s error_code=%s",
                        failed.input_batch_id,
                        event.event_id,
                        error_code,
                    )
            except Exception:
                logger.exception(
                    "ingress_reserved_draft_failure_recovery_failed "
                    "session_id=%s source_message_id=%s original_error_type=%s",
                    session_id,
                    envelope.source_message_id,
                    type(error).__name__,
                )
            raise
