"""Gateway-facing facade for attachment streaming and delivery receipts."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..artifacts import (
    ArtifactAccessError,
    ArtifactDeliveryError,
    ArtifactDeliveryRef,
)
from ..core.models import ClientType, UnifiedResponse
from ..ingress import (
    ClientInputEnvelope,
    CommittedInputBatch,
    IngressConflictError,
    IngressNotFoundError,
    InputGroupingMode,
    InputSubmissionResult,
    resolve_input_grouping,
)
from ..storage.errors import StorageStreamSourceError

if TYPE_CHECKING:
    from ..core.message_processor import MessageProcessor


logger = logging.getLogger("Gateway.ArtifactTransport")


class AttachmentProviderError(StorageStreamSourceError):
    """A closed attachment provider could not return exact source bytes."""


class AttachmentStreamProvider(Protocol):
    async def open_stream(
        self,
        locator: str,
        *,
        max_size_bytes: int,
    ) -> AsyncIterator[bytes]:
        ...


class DeliveryReceiptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    client_type: ClientType
    receipt: dict[str, Any] = Field(default_factory=dict)

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("session_id must not be empty")
        return normalized


class DeliveryFailureRequest(DeliveryReceiptRequest):
    error: str
    ambiguous: bool = False

    @field_validator("error")
    @classmethod
    def validate_error(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("error must not be empty")
        return normalized[:2_000]


class RunCommittedBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    progress_locale: str = "ru"

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("session_id must not be empty")
        return normalized


class CommitGroupedBatchRequest(RunCommittedBatchRequest):
    """Commit one already-ingested grouped draft and optionally run it."""

    run: bool = False


class ArtifactTransportFacade:
    """Coordinate Gateway IO while keeping transport outside artifact domain."""

    def __init__(
        self,
        *,
        api,
        message_processor: "MessageProcessor",
        providers: Mapping[str, AttachmentStreamProvider] | None = None,
    ) -> None:
        self.api = api
        self.message_processor = message_processor
        self.providers = dict(providers or {})

    @staticmethod
    def session_id_for(envelope: ClientInputEnvelope) -> str:
        conversation = envelope.conversation.conversation_id
        thread = envelope.conversation.thread_id
        suffix = f":thread:{thread}" if thread else ""
        return f"{envelope.client_type.value}:conversation:{conversation}{suffix}"

    async def submit_envelope(
        self,
        envelope: ClientInputEnvelope,
        *,
        upload_streams: Mapping[str, AsyncIterator[bytes]] | None = None,
    ) -> InputSubmissionResult:
        started = time.monotonic()
        session_id = self.session_id_for(envelope)
        grouping = resolve_input_grouping(envelope)
        logger.info(
            "gateway_ingress_started client_type=%s session_id=%s "
            "source_message_id=%s source_group_id=%s grouping_mode=%s "
            "attachment_count=%s text_part_count=%s",
            envelope.client_type.value,
            session_id,
            envelope.source_message_id,
            envelope.source_group_id,
            grouping.mode.value,
            len(envelope.attachment_slots),
            len(envelope.text_parts),
        )
        streams = dict(upload_streams or {})
        try:
            for slot in envelope.attachment_slots:
                if slot.slot_id in streams:
                    continue
                locator = slot.transport_locator
                if locator is None:
                    raise AttachmentProviderError(
                        f"No upload stream or provider locator for {slot.slot_id}"
                    )
                provider = self.providers.get(locator.provider)
                if provider is None:
                    raise AttachmentProviderError(
                        f"Attachment provider {locator.provider!r} is not configured"
                    )
                logger.info(
                    "gateway_attachment_stream_opening provider=%s slot_id=%s "
                    "filename=%s session_id=%s source_group_id=%s",
                    locator.provider,
                    slot.slot_id,
                    slot.original_filename,
                    session_id,
                    envelope.source_group_id,
                )
                streams[slot.slot_id] = await provider.open_stream(
                    locator.locator,
                    max_size_bytes=self.api.artifact_config.max_artifact_size_bytes,
                )

            result = await self.api.ingress_services.ingress_service.submit_atomic(
                envelope,
                session_id=session_id,
                upload_streams=streams,
                grouping_mode=grouping.mode,
                grouping_key=grouping.key,
            )
            logger.info(
                "gateway_ingress_finished input_batch_id=%s event_id=%s "
                "session_id=%s state=%s duplicate=%s error_code=%s duration_ms=%s",
                result.input_batch_id,
                result.event_id,
                session_id,
                result.state,
                result.duplicate,
                result.error_code,
                round((time.monotonic() - started) * 1000),
            )
            return result
        except AttachmentProviderError as error:
            await self._mark_provider_stream_failed(envelope)
            logger.exception(
                "gateway_ingress_provider_failed session_id=%s "
                "source_message_id=%s source_group_id=%s error_type=%s "
                "duration_ms=%s",
                session_id,
                envelope.source_message_id,
                envelope.source_group_id,
                type(error).__name__,
                round((time.monotonic() - started) * 1000),
            )
            raise
        except Exception as error:
            logger.exception(
                "gateway_ingress_failed session_id=%s source_message_id=%s "
                "source_group_id=%s error_type=%s duration_ms=%s",
                session_id,
                envelope.source_message_id,
                envelope.source_group_id,
                type(error).__name__,
                round((time.monotonic() - started) * 1000),
            )
            raise

    async def _mark_provider_stream_failed(
        self,
        envelope: ClientInputEnvelope,
    ) -> None:
        """Close a partially ingested draft without hiding storage failures."""
        event, _ = await self.api.ingress_services.event_store.save_if_absent(
            envelope
        )
        draft, committed = await self.api.ingress_services.batch_store.find_by_event(
            event.event_id
        )
        if draft is None or committed is not None:
            return
        await self.api.ingress_services.batch_store.fail(
            draft.input_batch_id,
            code="attachment_stream_failed",
        )
        logger.warning(
            "gateway_ingress_draft_failed input_batch_id=%s event_id=%s "
            "failure_code=attachment_stream_failed",
            draft.input_batch_id,
            event.event_id,
        )

    async def _commit_grouped_batch_application(
        self,
        input_batch_id: str,
        *,
        session_id: str,
    ) -> tuple[CommittedInputBatch, bool, tuple | None]:
        """Commit one complete grouped draft; repeated calls are idempotent."""
        store = self.api.ingress_services.batch_store
        draft = await store.get_draft(input_batch_id)
        counts = _attachment_state_counts(draft)
        logger.info(
            "gateway_batch_commit_requested input_batch_id=%s session_id=%s "
            "draft_state=%s stored=%s ingesting=%s pending=%s failed=%s",
            input_batch_id,
            session_id,
            draft.state.value,
            counts["stored"],
            counts["ingesting"],
            counts["pending"],
            counts["failed"],
        )
        if draft.session_id != session_id:
            logger.warning(
                "gateway_batch_commit_rejected input_batch_id=%s "
                "reason=session_mismatch",
                input_batch_id,
            )
            raise ArtifactAccessError("Input batch belongs to another session")
        if draft.grouping_mode == InputGroupingMode.ATOMIC:
            logger.warning(
                "gateway_batch_commit_rejected input_batch_id=%s "
                "reason=atomic_batch",
                input_batch_id,
            )
            raise IngressConflictError(
                "Atomic input batches are committed during submission"
            )

        try:
            committed = await store.get_committed(input_batch_id)
        except IngressNotFoundError:
            pass
        else:
            logger.info(
                "gateway_batch_commit_duplicate input_batch_id=%s "
                "artifact_count=%s",
                input_batch_id,
                len(committed.artifact_refs),
            )
            return committed, True, None

        ingress_service = self.api.ingress_services.ingress_service
        if not hasattr(ingress_service, "commit_batch"):
            logger.warning(
                "gateway_batch_commit_rejected input_batch_id=%s "
                "reason=grouped_store_unavailable",
                input_batch_id,
            )
            raise IngressConflictError(
                "Grouped input batch store is not configured"
            )
        try:
            application_commit = getattr(
                ingress_service,
                "commit_batch_application_result",
                None,
            )
            if application_commit is None:
                committed, duplicate = await ingress_service.commit_batch_result(
                    input_batch_id,
                    session_id=session_id,
                    reason="explicit_client_commit",
                )
                presentation_result = None
            else:
                (
                    committed,
                    duplicate,
                    presentation_result,
                ) = await application_commit(
                    input_batch_id,
                    session_id=session_id,
                    reason="explicit_client_commit",
                )
        except Exception as error:
            latest = await store.get_draft(input_batch_id)
            counts = _attachment_state_counts(latest)
            logger.exception(
                "gateway_batch_commit_rejected input_batch_id=%s "
                "draft_state=%s stored=%s ingesting=%s pending=%s failed=%s "
                "error_type=%s error=%s",
                input_batch_id,
                latest.state.value,
                counts["stored"],
                counts["ingesting"],
                counts["pending"],
                counts["failed"],
                type(error).__name__,
                str(error),
            )
            raise
        logger.info(
            "gateway_batch_commit_finished input_batch_id=%s duplicate=%s "
            "artifact_count=%s text_part_count=%s",
            input_batch_id,
            duplicate,
            len(committed.artifact_refs),
            len(committed.text_parts),
        )
        return committed, duplicate, presentation_result

    async def commit_grouped_batch(
        self,
        input_batch_id: str,
        *,
        session_id: str,
    ) -> tuple[CommittedInputBatch, bool]:
        """Compatibility projection without structured presentation details."""
        committed, duplicate, _ = await self._commit_grouped_batch_application(
            input_batch_id,
            session_id=session_id,
        )
        return committed, duplicate

    async def commit_grouped_batch_with_presentation(
        self,
        input_batch_id: str,
        *,
        session_id: str,
    ) -> tuple[CommittedInputBatch, bool, tuple | None]:
        """Commit a grouped batch and return the coordinator's ack event/ref."""
        return await self._commit_grouped_batch_application(
            input_batch_id,
            session_id=session_id,
        )

    async def run_committed_batch(
        self,
        input_batch_id: str,
        *,
        session_id: str,
        progress_callback=None,
        progress_locale: str = "ru",
    ) -> UnifiedResponse:
        batch = await self.api.ingress_services.batch_store.get_committed(
            input_batch_id
        )
        if batch.session_id != session_id:
            raise ArtifactAccessError("Input batch belongs to another session")
        logger.info(
            "gateway_agent_batch_started input_batch_id=%s session_id=%s "
            "artifact_count=%s text_part_count=%s",
            input_batch_id,
            session_id,
            len(batch.artifact_refs),
            len(batch.text_parts),
        )
        response = await self.message_processor.process_committed_batch(
            batch,
            progress_callback=progress_callback,
            progress_locale=progress_locale,
        )
        logger.info(
            "gateway_agent_batch_finished input_batch_id=%s session_id=%s",
            input_batch_id,
            session_id,
        )
        return response

    async def get_delivery_ref(
        self,
        delivery_id: str,
        *,
        session_id: str,
        client_type: ClientType,
    ) -> ArtifactDeliveryRef:
        record = await self.api.artifact_services.delivery_store.get(delivery_id)
        self._authorize_delivery(
            record,
            session_id=session_id,
            client_type=client_type,
        )
        self._authorize_output_owner(record, output_batch_id=None)
        return record.public_ref()

    async def claim_delivery(
        self,
        delivery_id: str,
        *,
        session_id: str,
        client_type: ClientType,
        output_batch_id: str | None = None,
    ) -> ArtifactDeliveryRef:
        record = await self.api.artifact_services.delivery_store.get(delivery_id)
        self._authorize_delivery(
            record,
            session_id=session_id,
            client_type=client_type,
        )
        self._authorize_output_owner(record, output_batch_id=output_batch_id)
        return await self.api.artifact_services.delivery_service.claim(delivery_id)

    async def open_delivery(
        self,
        delivery_id: str,
        *,
        session_id: str,
        client_type: ClientType,
        output_batch_id: str | None = None,
    ) -> AsyncIterator[bytes]:
        record = await self.api.artifact_services.delivery_store.get(delivery_id)
        self._authorize_delivery(
            record,
            session_id=session_id,
            client_type=client_type,
        )
        self._authorize_output_owner(record, output_batch_id=output_batch_id)
        return self.api.artifact_services.delivery_service.iter_content(
            delivery_id,
            session_id=session_id,
            client_type=client_type.value,
        )

    async def complete_delivery(
        self,
        delivery_id: str,
        request: DeliveryReceiptRequest,
    ) -> ArtifactDeliveryRef:
        record = await self.api.artifact_services.delivery_store.get(delivery_id)
        self._authorize_delivery(
            record,
            session_id=request.session_id,
            client_type=request.client_type,
        )
        self._reject_legacy_completion(record)
        return await self.api.artifact_services.delivery_service.complete(
            delivery_id,
            receipt=request.receipt,
        )

    async def fail_delivery(
        self,
        delivery_id: str,
        request: DeliveryFailureRequest,
    ) -> ArtifactDeliveryRef:
        record = await self.api.artifact_services.delivery_store.get(delivery_id)
        self._authorize_delivery(
            record,
            session_id=request.session_id,
            client_type=request.client_type,
        )
        self._reject_legacy_completion(record)
        return await self.api.artifact_services.delivery_service.fail(
            delivery_id,
            error=request.error,
            ambiguous=request.ambiguous,
            receipt=request.receipt,
        )

    @staticmethod
    def _authorize_delivery(record, *, session_id: str, client_type: ClientType) -> None:
        if (
            record.session_id != session_id
            or record.client_type != client_type.value
        ):
            raise ArtifactAccessError(
                "Delivery is outside the current client authority"
            )

    @staticmethod
    def _authorize_output_owner(
        record,
        *,
        output_batch_id: str | None,
    ) -> None:
        if record.output_batch_id is None:
            if output_batch_id is not None:
                raise ArtifactDeliveryError(
                    "Delivery is not owned by the requested OutputBatch"
                )
            return
        if record.output_batch_id != output_batch_id:
            raise ArtifactDeliveryError(
                "Output-owned delivery requires exact OutputBatch authority"
            )

    @staticmethod
    def _reject_legacy_completion(record) -> None:
        if record.output_batch_id is not None:
            raise ArtifactDeliveryError(
                "Output-owned delivery requires aggregate OutputBatch receipt"
            )


def _attachment_state_counts(draft: Any) -> dict[str, int]:
    counts = {"stored": 0, "ingesting": 0, "pending": 0, "failed": 0}
    for item in getattr(draft, "attachment_parts", ()):
        raw = getattr(getattr(item, "state", None), "value", None)
        state = str(raw or getattr(item, "state", "pending"))
        if state in counts:
            counts[state] += 1
        else:
            counts["pending"] += 1
    return counts
