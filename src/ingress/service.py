"""Streaming client ingress from durable event to committed input batch."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

from ..artifacts import (
    ArtifactConfigType,
    ArtifactContentKind,
    ArtifactProvenance,
    ArtifactPurpose,
    ArtifactServices,
    ArtifactValidationError,
)
from ..artifacts.errors import (
    ArtifactIntegrityError,
    ArtifactLimitError,
    ArtifactStorageError,
)
from ..storage.interfaces import ContentStore
from .config import IngressConfigType
from .models import (
    ClientInputEnvelope,
    CommittedInputBatch,
    InputAttachmentState,
    InputBatchDraftState,
    InputGroupingMode,
    InputSubmissionResult,
)
from .store import (
    FileSystemIngressEventStore,
    FileSystemInputBatchStore,
    IngressConflictError,
)


class IngressValidationError(RuntimeError):
    """Client input cannot become one complete committed batch."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ArtifactIngressService:
    """Persist transport input without exposing partial batches to the agent."""

    def __init__(
        self,
        *,
        config: IngressConfigType,
        artifact_config: ArtifactConfigType,
        content_store: ContentStore,
        artifact_services: ArtifactServices,
        event_store: FileSystemIngressEventStore,
        batch_store: FileSystemInputBatchStore,
    ) -> None:
        self.config = config
        self.artifact_config = artifact_config
        self.content_store = content_store
        self.artifact_services = artifact_services
        self.event_store = event_store
        self.batch_store = batch_store

    async def submit_atomic(
        self,
        envelope: ClientInputEnvelope,
        *,
        session_id: str,
        upload_streams: Mapping[str, AsyncIterator[bytes]] | None = None,
        grouping_mode: InputGroupingMode = InputGroupingMode.ATOMIC,
        grouping_key: str | None = None,
    ) -> InputSubmissionResult:
        """Durably ingest one complete text/files envelope and commit it once."""
        if not self.config.enabled:
            raise IngressValidationError(
                "ingress_disabled",
                "Client file ingress is disabled.",
            )
        self._validate_envelope_limits(envelope)
        event, duplicate_event = await self.event_store.save_if_absent(envelope)
        draft, duplicate_batch = await self.batch_store.create_for_event(
            event,
            session_id=session_id,
            grouping_mode=grouping_mode,
            grouping_key=(
                grouping_key
                or f"{event.client_type.value}:{event.event_id}"
            ),
        )

        try:
            committed = await self.batch_store.get_committed(
                draft.input_batch_id
            )
        except Exception as error:
            from .store import IngressNotFoundError

            if not isinstance(error, IngressNotFoundError):
                raise
        else:
            return InputSubmissionResult(
                event_id=event.event_id,
                input_batch_id=committed.input_batch_id,
                state="committed",
                duplicate=True,
                committed_batch=committed,
            )

        if draft.state == InputBatchDraftState.FAILED:
            return InputSubmissionResult(
                event_id=event.event_id,
                input_batch_id=draft.input_batch_id,
                state="failed",
                duplicate=duplicate_event or duplicate_batch,
                error_code=draft.failure_code,
            )

        streams = dict(upload_streams or {})
        expected_slot_ids = {
            slot.slot_id for slot in event.attachment_slots
        }
        if set(streams) - expected_slot_ids:
            await self.batch_store.fail(
                draft.input_batch_id,
                code="unexpected_upload_slot",
            )
            return InputSubmissionResult(
                event_id=event.event_id,
                input_batch_id=draft.input_batch_id,
                state="failed",
                duplicate=duplicate_event or duplicate_batch,
                error_code="unexpected_upload_slot",
            )

        await self.batch_store.begin_ingestion(draft.input_batch_id)
        total_bytes = sum(
            int(item.size_bytes or 0)
            for item in draft.attachment_parts
            if item.state == InputAttachmentState.STORED
        )

        try:
            for slot in event.attachment_slots:
                current = await self.batch_store.get_draft(
                    draft.input_batch_id
                )
                existing = next(
                    item
                    for item in current.attachment_parts
                    if item.slot_id == slot.slot_id
                )
                if existing.state == InputAttachmentState.STORED:
                    continue
                stream = streams.get(slot.slot_id)
                if stream is None:
                    raise IngressValidationError(
                        "missing_upload_stream",
                        f"Attachment stream is missing for {slot.slot_id}",
                    )
                await self.batch_store.mark_attachment_ingesting(
                    draft.input_batch_id,
                    slot.slot_id,
                )
                size = await self._ingest_slot(
                    event=event,
                    input_batch_id=draft.input_batch_id,
                    session_id=session_id,
                    slot=slot,
                    stream=stream,
                )
                total_bytes += size
                if total_bytes > self.config.max_batch_total_bytes:
                    raise IngressValidationError(
                        "input_batch_too_large",
                        "Input batch exceeds the configured total byte limit.",
                    )

            committed = await self.batch_store.commit(
                draft.input_batch_id,
                reason=(
                    "atomic_upload"
                    if event.attachment_slots
                    else "immediate_text"
                ),
            )
            return InputSubmissionResult(
                event_id=event.event_id,
                input_batch_id=committed.input_batch_id,
                state="committed",
                duplicate=duplicate_event or duplicate_batch,
                committed_batch=committed,
            )
        except IngressValidationError as error:
            await self.batch_store.fail(
                draft.input_batch_id,
                code=error.code,
            )
            return InputSubmissionResult(
                event_id=event.event_id,
                input_batch_id=draft.input_batch_id,
                state="failed",
                duplicate=duplicate_event or duplicate_batch,
                error_code=error.code,
            )
        except (ArtifactValidationError, ArtifactLimitError) as error:
            code = getattr(error, "code", "artifact_ingress_validation_failed")
            await self.batch_store.fail(
                draft.input_batch_id,
                code=str(code),
            )
            return InputSubmissionResult(
                event_id=event.event_id,
                input_batch_id=draft.input_batch_id,
                state="failed",
                duplicate=duplicate_event or duplicate_batch,
                error_code=str(code),
            )
        except (ArtifactStorageError, ArtifactIntegrityError):
            # Transport must receive a retryable infrastructure failure; the
            # durable draft remains non-committed and invisible to the agent.
            raise

    async def _ingest_slot(
        self,
        *,
        event,
        input_batch_id: str,
        session_id: str,
        slot,
        stream: AsyncIterator[bytes],
    ) -> int:
        filename = slot.original_filename or f"upload-{slot.slot_id}.bin"
        preliminary = self.artifact_services.format_registry.detect(
            filename=filename,
            declared_mime_type=slot.declared_mime_type,
            prefix=b"",
        )
        preliminary_spec = self.artifact_services.format_registry.get(
            preliminary.format_id
        )
        content_ref = await self.content_store.save_stream(
            stream,
            source_type="user_file_content",
            source_name=filename,
            mime_type=(
                slot.declared_mime_type
                or preliminary.detected_mime_type
            ),
            encoding=(
                preliminary_spec.default_encoding
                if preliminary.content_kind == ArtifactContentKind.NATIVE_TEXT
                else None
            ),
            cycle_id=f"input_batch:{input_batch_id}",
            metadata={
                "input_batch_id": input_batch_id,
                "ingress_event_id": event.event_id,
                "attachment_slot_id": slot.slot_id,
                "client_type": event.client_type.value,
                "untrusted": True,
            },
            max_size_bytes=self.artifact_config.max_artifact_size_bytes,
        )
        prefix_range = await self.content_store.read_range(
            content_ref.content_id,
            offset=0,
            length=min(content_ref.size_bytes, 64 * 1024),
        )
        detected = self.artifact_services.format_registry.detect(
            filename=filename,
            declared_mime_type=slot.declared_mime_type,
            prefix=prefix_range.data,
        )
        # Generic ZIP signature must not erase a reliable Office extension/MIME
        # when container entry inspection is deliberately deferred in v0.4.
        if (
            detected.format_id == "zip"
            and preliminary.format_id in {"docx", "xlsx", "pptx"}
        ):
            detected = preliminary
        if (
            detected.format_id == "opaque_binary"
            and not self.artifact_config.allow_opaque_binary
        ):
            raise IngressValidationError(
                "opaque_binary_not_allowed",
                "The uploaded file format is not allowed by artifact policy.",
            )
        spec = self.artifact_services.format_registry.get(detected.format_id)
        lineage, version = await self.artifact_services.artifact_store.create_lineage(
            session_id=session_id,
            cycle_id=f"input_batch:{input_batch_id}",
            content_id=content_ref.content_id,
            filename=filename,
            format_id=detected.format_id,
            detected_mime_type=detected.detected_mime_type,
            provenance=ArtifactProvenance(
                origin="user_upload",
                creator="user",
                input_batch_id=input_batch_id,
                source_content_ids=[content_ref.content_id],
                client_type=event.client_type.value,
                source_message_ids=[event.source_message_id],
                operation="client_artifact_ingress",
            ),
            purpose=ArtifactPurpose.INPUT,
            declared_mime_type=slot.declared_mime_type,
            encoding=spec.default_encoding,
            title=filename,
            metadata={
                "ingress_event_id": event.event_id,
                "attachment_slot_id": slot.slot_id,
                "format_detection": detected.model_dump(mode="json"),
                "untrusted": True,
            },
        )
        await self.batch_store.mark_attachment_stored(
            input_batch_id,
            slot.slot_id,
            content_id=content_ref.content_id,
            artifact_id=version.artifact_id,
            detected_format_id=version.format_id,
            detected_mime_type=version.detected_mime_type,
            size_bytes=version.size_bytes,
            content_hash=version.content_hash,
        )
        return version.size_bytes

    def _validate_envelope_limits(self, envelope: ClientInputEnvelope) -> None:
        if len(envelope.text_parts) > self.config.max_text_parts_per_batch:
            raise IngressValidationError(
                "too_many_text_parts",
                "Input contains too many text parts.",
            )
        if len(envelope.attachment_slots) > self.config.max_attachments_per_batch:
            raise IngressValidationError(
                "too_many_attachments",
                "Input contains too many attachments.",
            )
        for part in envelope.text_parts:
            if len(part.text) > self.config.max_text_part_chars:
                raise IngressValidationError(
                    "text_part_too_large",
                    "One input text part exceeds the configured character limit.",
                )
        declared_total = sum(
            int(slot.declared_size_bytes or 0)
            for slot in envelope.attachment_slots
        )
        if declared_total > self.config.max_batch_total_bytes:
            raise IngressValidationError(
                "declared_input_batch_too_large",
                "Declared attachment sizes exceed the batch limit.",
            )
