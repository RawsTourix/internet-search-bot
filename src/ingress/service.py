"""Streaming client ingress from durable event to committed input batch."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Mapping

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
from ..interaction.capabilities import (
    build_cli_capability_declaration,
    build_telegram_capability_declaration,
    build_web_capability_declaration,
)
from ..interaction.capability_store import FileSystemCapabilitySnapshotStore
from ..interaction.presentation_service import InputPresentationCoordinator
from ..interaction.presentation import PresentationAckPolicy
from ..localization.service import LocalizationService
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
    IngressNotFoundError,
)
from .semantic_limits import (
    SemanticInputLimitError,
    validate_semantic_parts,
)


logger = logging.getLogger("API.Ingress")


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
        capability_store: FileSystemCapabilitySnapshotStore | None = None,
        localization_service: LocalizationService | None = None,
        presentation_coordinator: InputPresentationCoordinator | None = None,
        telegram_document_grouping: bool = True,
        telegram_message_editing: bool = True,
    ) -> None:
        self.config = config
        self.artifact_config = artifact_config
        self.content_store = content_store
        self.artifact_services = artifact_services
        self.event_store = event_store
        self.batch_store = batch_store
        self.capability_store = capability_store
        self.localization_service = localization_service
        self.presentation_coordinator = presentation_coordinator
        self.telegram_document_grouping = telegram_document_grouping
        self.telegram_message_editing = telegram_message_editing

    async def submit_atomic(
        self,
        envelope: ClientInputEnvelope,
        *,
        session_id: str,
        upload_streams: Mapping[str, AsyncIterator[bytes]] | None = None,
        grouping_mode: InputGroupingMode = InputGroupingMode.ATOMIC,
        grouping_key: str | None = None,
    ) -> InputSubmissionResult:
        """Ingest one event; grouped Telegram drafts remain uncommitted."""
        started = time.monotonic()
        logger.info(
            "api_ingress_event_started session_id=%s client_type=%s "
            "source_message_id=%s source_group_id=%s grouping_mode=%s "
            "attachment_count=%s text_part_count=%s",
            session_id,
            envelope.client_type.value,
            envelope.source_message_id,
            envelope.source_group_id,
            grouping_mode.value,
            len(envelope.attachment_slots),
            len(envelope.text_parts),
        )
        if not self.config.enabled:
            raise IngressValidationError(
                "ingress_disabled",
                "Client file ingress is disabled.",
            )
        self._validate_envelope_limits(envelope)
        capability_snapshot, resolved_locale = await self._resolve_interaction(
            envelope
        )
        event, duplicate_event = await self.event_store.save_if_absent(
            envelope,
            capability_snapshot=capability_snapshot,
            resolved_locale=resolved_locale,
        )
        draft, duplicate_batch = await self.batch_store.create_for_event(
            event,
            session_id=session_id,
            grouping_mode=grouping_mode,
            grouping_key=(
                grouping_key
                or f"{event.client_type.value}:{event.event_id}"
            ),
        )
        logger.info(
            "api_ingress_draft_resolved input_batch_id=%s event_id=%s "
            "draft_state=%s duplicate_event=%s duplicate_batch=%s",
            draft.input_batch_id,
            event.event_id,
            draft.state.value,
            duplicate_event,
            duplicate_batch,
        )

        try:
            committed = await self.batch_store.get_committed(
                draft.input_batch_id
            )
        except IngressNotFoundError:
            committed = None
        if committed is not None:
            result = InputSubmissionResult(
                event_id=event.event_id,
                input_batch_id=committed.input_batch_id,
                state="committed",
                duplicate=True,
                committed_batch=committed,
            )
            result = await self._decorate_result(result, envelope=envelope)
            self._log_submission_result(result, started=started)
            return result

        if draft.state == InputBatchDraftState.FAILED:
            result = InputSubmissionResult(
                event_id=event.event_id,
                input_batch_id=draft.input_batch_id,
                state="failed",
                duplicate=duplicate_event or duplicate_batch,
                error_code=draft.failure_code,
            )
            result = await self._decorate_result(result, envelope=envelope)
            self._log_submission_result(result, started=started)
            return result

        streams = dict(upload_streams or {})
        expected_slot_ids = {
            slot.slot_id for slot in event.attachment_slots
        }
        if set(streams) - expected_slot_ids:
            await self.batch_store.fail(
                draft.input_batch_id,
                code="unexpected_upload_slot",
            )
            result = InputSubmissionResult(
                event_id=event.event_id,
                input_batch_id=draft.input_batch_id,
                state="failed",
                duplicate=duplicate_event or duplicate_batch,
                error_code="unexpected_upload_slot",
            )
            result = await self._decorate_result(result, envelope=envelope)
            self._log_submission_result(result, started=started)
            return result

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
                    logger.info(
                        "api_ingress_attachment_reused input_batch_id=%s "
                        "event_id=%s slot_id=%s filename=%s",
                        draft.input_batch_id,
                        event.event_id,
                        slot.slot_id,
                        slot.original_filename,
                    )
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
                logger.info(
                    "api_ingress_attachment_started input_batch_id=%s "
                    "event_id=%s slot_id=%s filename=%s declared_size=%s",
                    draft.input_batch_id,
                    event.event_id,
                    slot.slot_id,
                    slot.original_filename,
                    slot.declared_size_bytes,
                )
                slot_started = time.monotonic()
                try:
                    size = await self._ingest_slot(
                        event=event,
                        input_batch_id=draft.input_batch_id,
                        session_id=session_id,
                        slot=slot,
                        stream=stream,
                    )
                except Exception as error:
                    logger.exception(
                        "api_ingress_attachment_failed input_batch_id=%s "
                        "event_id=%s slot_id=%s filename=%s error_type=%s "
                        "duration_ms=%s",
                        draft.input_batch_id,
                        event.event_id,
                        slot.slot_id,
                        slot.original_filename,
                        type(error).__name__,
                        round((time.monotonic() - slot_started) * 1000),
                    )
                    raise
                logger.info(
                    "api_ingress_attachment_stored input_batch_id=%s "
                    "event_id=%s slot_id=%s filename=%s size_bytes=%s "
                    "duration_ms=%s",
                    draft.input_batch_id,
                    event.event_id,
                    slot.slot_id,
                    slot.original_filename,
                    size,
                    round((time.monotonic() - slot_started) * 1000),
                )
                total_bytes += size
                if total_bytes > self.config.max_batch_total_bytes:
                    raise IngressValidationError(
                        "input_batch_too_large",
                        "Input batch exceeds the configured total byte limit.",
                    )

            if grouping_mode != InputGroupingMode.ATOMIC:
                mark_collecting = getattr(
                    self.batch_store,
                    "mark_collecting",
                    None,
                )
                if mark_collecting is None:
                    raise ArtifactStorageError(
                        "Grouped input requires a grouped batch store"
                    )
                await mark_collecting(draft.input_batch_id)
                result = InputSubmissionResult(
                    event_id=event.event_id,
                    input_batch_id=draft.input_batch_id,
                    state="collecting",
                    duplicate=duplicate_event or duplicate_batch,
                )
                result = await self._decorate_result(result, envelope=envelope)
                # Presentation and durable metadata reads may take longer than
                # the media-group quiet timeout on slow filesystems. Reset the
                # authoritative deadline immediately before returning so the
                # transport receives the full advertised join window.
                defer_commit = getattr(
                    self.batch_store,
                    "defer_commit",
                    None,
                )
                if defer_commit is not None:
                    await defer_commit(draft.input_batch_id)
                self._log_submission_result(result, started=started)
                return result

            committed = await self.batch_store.commit(
                draft.input_batch_id,
                reason=(
                    "atomic_upload"
                    if event.attachment_slots
                    else "immediate_text"
                ),
            )
            result = InputSubmissionResult(
                event_id=event.event_id,
                input_batch_id=committed.input_batch_id,
                state="committed",
                duplicate=duplicate_event or duplicate_batch,
                committed_batch=committed,
            )
            result = await self._decorate_result(result, envelope=envelope)
            self._log_submission_result(result, started=started)
            return result
        except IngressValidationError as error:
            await self.batch_store.fail(
                draft.input_batch_id,
                code=error.code,
            )
            result = InputSubmissionResult(
                event_id=event.event_id,
                input_batch_id=draft.input_batch_id,
                state="failed",
                duplicate=duplicate_event or duplicate_batch,
                error_code=error.code,
            )
            result = await self._decorate_result(result, envelope=envelope)
            self._log_submission_result(result, started=started)
            return result
        except (ArtifactValidationError, ArtifactLimitError) as error:
            code = getattr(error, "code", "artifact_ingress_validation_failed")
            await self.batch_store.fail(
                draft.input_batch_id,
                code=str(code),
            )
            result = InputSubmissionResult(
                event_id=event.event_id,
                input_batch_id=draft.input_batch_id,
                state="failed",
                duplicate=duplicate_event or duplicate_batch,
                error_code=str(code),
            )
            result = await self._decorate_result(result, envelope=envelope)
            self._log_submission_result(result, started=started)
            return result
        except (ArtifactStorageError, ArtifactIntegrityError) as error:
            logger.exception(
                "api_ingress_storage_failed input_batch_id=%s event_id=%s "
                "error_type=%s duration_ms=%s",
                draft.input_batch_id,
                event.event_id,
                type(error).__name__,
                round((time.monotonic() - started) * 1000),
            )
            # The draft remains non-committed and invisible to the agent.
            raise

    async def commit_batch_application_result(
        self,
        input_batch_id: str,
        *,
        session_id: str,
        reason: str,
    ) -> tuple[CommittedInputBatch, bool, tuple | None]:
        """Seal a grouped draft and return its presentation application result."""
        draft = await self.batch_store.get_draft(input_batch_id)
        counts = _attachment_state_counts(draft)
        logger.info(
            "api_ingress_commit_requested input_batch_id=%s session_id=%s "
            "reason=%s draft_state=%s stored=%s ingesting=%s pending=%s failed=%s",
            input_batch_id,
            session_id,
            reason,
            draft.state.value,
            counts["stored"],
            counts["ingesting"],
            counts["pending"],
            counts["failed"],
        )
        if draft.session_id != session_id:
            raise IngressConflictError("Input batch belongs to another session")
        if draft.state == InputBatchDraftState.FAILED:
            raise IngressConflictError("Failed input batch cannot be committed")
        if any(
            item.state != InputAttachmentState.STORED
            for item in draft.attachment_parts
        ):
            logger.warning(
                "api_ingress_commit_deferred input_batch_id=%s stored=%s "
                "ingesting=%s pending=%s failed=%s",
                input_batch_id,
                counts["stored"],
                counts["ingesting"],
                counts["pending"],
                counts["failed"],
            )
            raise IngressConflictError(
                "All attachment slots must be stored before commit"
            )
        grouped_commit = getattr(self.batch_store, "commit_batch", None)
        if grouped_commit is not None:
            committed, duplicate = await grouped_commit(
                input_batch_id,
                session_id=session_id,
                reason=reason,
            )
        else:
            committed = await self.batch_store.commit(
                input_batch_id,
                reason=reason,
            )
            duplicate = False
        logger.info(
            "api_ingress_commit_finished input_batch_id=%s artifact_count=%s "
            "text_part_count=%s",
            input_batch_id,
            len(committed.artifact_refs),
            len(committed.text_parts),
        )
        presentation_result = None
        if self.presentation_coordinator is not None:
            presentation_result = (
                await self.presentation_coordinator.finalize_batch(
                    input_batch_id=input_batch_id,
                    state="committed",
                    file_count=len(draft.attachment_parts),
                    text_part_count=len(draft.text_parts),
                    response_anchor=committed.response_anchor,
                )
            )
        return committed, duplicate, presentation_result

    async def commit_batch_result(
        self,
        input_batch_id: str,
        *,
        session_id: str,
        reason: str,
    ) -> tuple[CommittedInputBatch, bool]:
        """Compatibility projection of the grouped application commit result."""
        committed, duplicate, _ = await self.commit_batch_application_result(
            input_batch_id,
            session_id=session_id,
            reason=reason,
        )
        return committed, duplicate

    async def commit_batch(
        self,
        input_batch_id: str,
        *,
        session_id: str,
        reason: str,
    ) -> CommittedInputBatch:
        committed, _ = await self.commit_batch_result(
            input_batch_id,
            session_id=session_id,
            reason=reason,
        )
        return committed

    async def commit_ready_drafts(self) -> list[CommittedInputBatch]:
        """Recovery/sweeper helper; it commits bytes but never starts the agent."""
        list_ready = getattr(self.batch_store, "list_ready_drafts", None)
        if list_ready is None:
            return []
        result: list[CommittedInputBatch] = []
        for draft in await list_ready():
            result.append(await self.commit_batch(
                draft.input_batch_id,
                session_id=draft.session_id,
                reason=(
                    "media_group_quiet_timeout"
                    if draft.grouping_mode == InputGroupingMode.MEDIA_GROUP
                    else "standalone_attachment_timeout"
                ),
            ))
        return result

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
        _, version = await self.artifact_services.artifact_store.create_lineage(
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
            artifact_lineage_id=version.artifact_lineage_id,
            version=version.version,
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
        try:
            validate_semantic_parts(envelope.semantic_parts, self.config)
        except SemanticInputLimitError as error:
            raise IngressValidationError(
                "semantic_input_limit_exceeded",
                str(error),
            ) from error

    async def _resolve_interaction(self, envelope: ClientInputEnvelope):
        locale = (
            self.localization_service.resolve_locale(
                explicit_locale=envelope.locale,
                transport_locale=envelope.transport_locale,
            )
            if self.localization_service is not None
            else envelope.locale
        )
        if self.capability_store is None:
            return None, locale
        if envelope.capability_snapshot_ref is not None:
            snapshot = await self.capability_store.get(
                envelope.capability_snapshot_ref.capability_snapshot_id
            )
            if (
                snapshot.fingerprint
                != envelope.capability_snapshot_ref.fingerprint
                or snapshot.client_instance_id != envelope.client_instance_id
                or snapshot.client_type != envelope.client_type.value
            ):
                raise IngressValidationError(
                    "capability_snapshot_mismatch",
                    "Capability snapshot does not match this client binding.",
                )
            return snapshot, locale
        declaration = envelope.capability_declaration
        if declaration is None:
            if envelope.client_type.value == "telegram":
                declaration = build_telegram_capability_declaration(
                    client_version=envelope.client_version,
                    document_grouping=self.telegram_document_grouping,
                    message_editing=self.telegram_message_editing,
                )
            elif envelope.client_type.value == "web":
                declaration = build_web_capability_declaration(
                    client_version=envelope.client_version
                )
            else:
                declaration = build_cli_capability_declaration(
                    client_version=envelope.client_version
                )
        snapshot, _ = await self.capability_store.resolve(
            declaration,
            client_type=envelope.client_type.value,
            client_instance_id=envelope.client_instance_id,
        )
        return snapshot, locale

    async def _decorate_result(
        self,
        result: InputSubmissionResult,
        *,
        envelope: ClientInputEnvelope,
    ) -> InputSubmissionResult:
        try:
            draft = await self.batch_store.get_draft(result.input_batch_id)
        except IngressNotFoundError:
            return result
        updates = {
            "response_anchor": draft.response_anchor,
            "file_count": len(draft.attachment_parts),
            "text_part_count": len(draft.text_parts),
        }
        if self.presentation_coordinator is not None:
            locale = draft.locale or (
                self.localization_service.config.default_locale
                if self.localization_service is not None
                else "ru"
            )
            binding_id = (
                envelope.client_binding_id
                or (
                    f"{envelope.client_type.value}:"
                    f"{envelope.client_instance_id}:"
                    f"{envelope.conversation.conversation_id}:"
                    f"{envelope.conversation.thread_id or '-'}"
                )
            )
            ack, event, ref = await self.presentation_coordinator.present(
                input_batch_id=result.input_batch_id,
                client_binding_id=binding_id,
                locale=locale,
                state=result.state,
                file_count=len(draft.attachment_parts),
                text_part_count=len(draft.text_parts),
                response_anchor=draft.response_anchor,
            )
            if (
                ack
                in {
                    PresentationAckPolicy.UPDATE_EXISTING,
                    PresentationAckPolicy.THROTTLED_UPDATE,
                }
                and (
                    draft.capability_snapshot is None
                    or "presentation.message_edit"
                    not in draft.capability_snapshot.features
                )
            ):
                ack = PresentationAckPolicy.SILENT
            updates.update(
                ack_policy=ack,
                presentation_event=event,
                presentation_ref=ref,
            )
        return result.model_copy(update=updates)

    @staticmethod
    def _log_submission_result(
        result: InputSubmissionResult,
        *,
        started: float,
    ) -> None:
        logger.info(
            "api_ingress_event_finished input_batch_id=%s event_id=%s "
            "state=%s duplicate=%s error_code=%s duration_ms=%s",
            result.input_batch_id,
            result.event_id,
            result.state,
            result.duplicate,
            result.error_code,
            round((time.monotonic() - started) * 1000),
        )


def _attachment_state_counts(draft) -> dict[str, int]:
    counts = {"stored": 0, "ingesting": 0, "pending": 0, "failed": 0}
    for item in draft.attachment_parts:
        raw = getattr(item.state, "value", item.state)
        state = str(raw)
        if state in counts:
            counts[state] += 1
        else:
            counts["pending"] += 1
    return counts
