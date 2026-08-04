"""Failure recovery around durable logical-input submission."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Mapping

from ..artifacts.errors import ArtifactIntegrityError, ArtifactStorageError
from .models import (
    ClientInputEnvelope,
    InputAttachmentState,
    InputGroupingMode,
    InputSubmissionResult,
)
from .startup_recovery import reconcile_ingress_after_restart
from .unified_service import UnifiedArtifactIngressService


logger = logging.getLogger("API.Ingress.Recovery")


class ResilientUnifiedArtifactIngressService(UnifiedArtifactIngressService):
    """Recover reserved drafts across runtime failures and process restarts."""

    async def commit_ready_drafts(self):
        """Use the existing API startup hook for shared ingress reconciliation."""

        report = await reconcile_ingress_after_restart(
            self,
            self.batch_store,
        )
        self.last_startup_recovery_report = report
        return [
            await self.batch_store.get_committed(input_batch_id)
            for input_batch_id in report.committed_input_batch_ids
        ]

    async def submit_atomic(
        self,
        envelope: ClientInputEnvelope,
        *,
        session_id: str,
        upload_streams: Mapping[str, AsyncIterator[bytes]] | None = None,
        grouping_mode: InputGroupingMode = InputGroupingMode.ATOMIC,
        grouping_key: str | None = None,
    ) -> InputSubmissionResult:
        started = time.monotonic()
        try:
            result = await super().submit_atomic(
                envelope,
                session_id=session_id,
                upload_streams=upload_streams,
                grouping_mode=grouping_mode,
                grouping_key=grouping_key,
            )
            await self._trace_submission(
                envelope=envelope,
                session_id=session_id,
                result=result,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            return result
        except (ArtifactStorageError, ArtifactIntegrityError) as error:
            error_code = (
                "artifact_ingress_integrity_failed"
                if isinstance(error, ArtifactIntegrityError)
                else "artifact_ingress_storage_failed"
            )
            recovered_batch_id: str | None = None
            recovered_event_id: str | None = None
            try:
                capability_snapshot, resolved_locale = (
                    await self._resolve_interaction(envelope)
                )
                event, _ = await self.event_store.save_if_absent(
                    envelope,
                    capability_snapshot=capability_snapshot,
                    resolved_locale=resolved_locale,
                )
                recovered_event_id = event.event_id
                draft, committed = await self.batch_store.find_by_event(
                    event.event_id
                )
                if draft is not None and committed is None:
                    recovered_batch_id = draft.input_batch_id
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
            await self._trace_failure(
                envelope=envelope,
                session_id=session_id,
                error=error,
                error_code=error_code,
                event_id=recovered_event_id,
                input_batch_id=recovered_batch_id,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            raise

    async def _trace_submission(
        self,
        *,
        envelope: ClientInputEnvelope,
        session_id: str,
        result: InputSubmissionResult,
        duration_ms: int,
    ) -> None:
        trace = getattr(self.artifact_services, "trace_service", None)
        if trace is None:
            return
        try:
            draft = await self.batch_store.get_draft(result.input_batch_id)
        except Exception as error:
            await trace.record(
                session_id=session_id,
                event_type="input_batch_trace_snapshot_failed",
                stage="ingress",
                status="failed",
                direction="inbound",
                correlation={
                    "ingress_event_id": result.event_id,
                    "input_batch_id": result.input_batch_id,
                },
                transport=self._trace_transport(envelope),
                metrics={"duration_ms": duration_ms},
                error=error,
                data={
                    "submission_state": result.state,
                    "duplicate": result.duplicate,
                },
            )
            return

        submission_status = (
            "failed" if result.state == "failed" else "succeeded"
        )
        await trace.record(
            session_id=session_id,
            event_type="input_batch_updated",
            stage="ingress",
            status=submission_status,
            direction="inbound",
            correlation={
                "ingress_event_id": result.event_id,
                "input_batch_id": result.input_batch_id,
            },
            transport=self._trace_transport(envelope),
            metrics={
                "duration_ms": duration_ms,
                "file_count": len(draft.attachment_parts),
                "text_part_count": len(draft.text_parts),
                "semantic_part_count": len(draft.semantic_parts),
            },
            error=(
                {
                    "error_type": "IngressSubmissionError",
                    "error_code": result.error_code,
                }
                if result.state == "failed"
                else None
            ),
            data={
                "submission_state": result.state,
                "draft_state": draft.state.value,
                "grouping_mode": draft.grouping_mode.value,
                "duplicate": result.duplicate,
            },
        )

        by_slot_id = {item.slot_id: item for item in draft.attachment_parts}
        for slot in envelope.attachment_slots:
            stored = by_slot_id.get(slot.slot_id)
            if stored is None:
                continue
            if stored.state == InputAttachmentState.STORED:
                event_type = "artifact_ingress_stored"
                status = "succeeded"
            elif stored.state == InputAttachmentState.FAILED:
                event_type = "artifact_ingress_failed"
                status = "failed"
            else:
                event_type = "artifact_ingress_observed"
                status = "observed"
            await trace.record(
                session_id=session_id,
                event_type=event_type,
                stage="ingress",
                status=status,
                direction="inbound",
                correlation={
                    "ingress_event_id": result.event_id,
                    "input_batch_id": result.input_batch_id,
                },
                transport=self._trace_transport(envelope),
                artifact={
                    "artifact_id": stored.artifact_id,
                    "artifact_lineage_id": stored.artifact_lineage_id,
                    "content_id": stored.content_id,
                    "filename": stored.original_filename,
                    "format_id": stored.detected_format_id,
                    "mime_type": stored.detected_mime_type,
                    "size_bytes": stored.size_bytes,
                    "content_hash": stored.content_hash,
                    "purpose": "input",
                    "version": stored.version,
                },
                error=(
                    {
                        "error_type": "ArtifactIngressError",
                        "error_code": stored.error_code,
                    }
                    if stored.state == InputAttachmentState.FAILED
                    else None
                ),
                data={
                    "attachment_slot_id": stored.slot_id,
                    "attachment_state": stored.state.value,
                    "declared_mime_type": stored.declared_mime_type,
                    "declared_size_bytes": stored.declared_size_bytes,
                },
            )

    async def _trace_failure(
        self,
        *,
        envelope: ClientInputEnvelope,
        session_id: str,
        error: BaseException,
        error_code: str,
        event_id: str | None,
        input_batch_id: str | None,
        duration_ms: int,
    ) -> None:
        trace = getattr(self.artifact_services, "trace_service", None)
        if trace is None:
            return
        await trace.record(
            session_id=session_id,
            event_type="artifact_ingress_failed",
            stage="ingress",
            status="failed",
            direction="inbound",
            correlation={
                "ingress_event_id": event_id,
                "input_batch_id": input_batch_id,
            },
            transport=self._trace_transport(envelope),
            metrics={
                "duration_ms": duration_ms,
                "attachment_count": len(envelope.attachment_slots),
                "text_part_count": len(envelope.text_parts),
            },
            error={
                "error_type": type(error).__name__,
                "error_code": error_code,
                "message": str(error),
            },
        )

    @staticmethod
    def _trace_transport(envelope: ClientInputEnvelope) -> dict:
        return {
            "client_type": envelope.client_type.value,
            "client_instance_id": envelope.client_instance_id,
            "conversation_id": envelope.conversation.conversation_id,
            "thread_id": envelope.conversation.thread_id,
            "source_update_id": envelope.source_update_id,
            "source_message_id": envelope.source_message_id,
            "source_group_id": envelope.source_group_id,
        }
