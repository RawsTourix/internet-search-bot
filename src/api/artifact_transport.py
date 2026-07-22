"""Gateway-facing facade for attachment streaming and delivery receipts."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..artifacts import ArtifactAccessError, ArtifactDeliveryRef
from ..core.message_processor import MessageProcessor
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


class AttachmentProviderError(RuntimeError):
    """A closed attachment provider could not return exact source bytes."""


class AttachmentStreamProvider(Protocol):
    async def open_stream(
        self,
        locator: str,
        *,
        max_size_bytes: int,
    ) -> AsyncIterator[bytes]:
        ...


@dataclass(slots=True)
class HttpAttachmentStreamProvider:
    """Fetch bytes from one fixed internal provider origin, never a client URL."""

    base_url: str
    token: str
    provider_name: str
    path_prefix: str = "/internal/files"
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 120.0

    async def open_stream(
        self,
        locator: str,
        *,
        max_size_bytes: int,
    ) -> AsyncIterator[bytes]:
        normalized = locator.strip()
        if not normalized or any(character in normalized for character in "\r\n"):
            raise AttachmentProviderError("Invalid attachment locator")
        url = (
            self.base_url.rstrip("/")
            + self.path_prefix.rstrip("/")
            + "/"
            + quote(normalized, safe="")
        )
        headers = {"X-File-Provider-Token": self.token}
        timeout = httpx.Timeout(
            connect=self.connect_timeout_seconds,
            read=self.read_timeout_seconds,
            write=30.0,
            pool=10.0,
        )

        async def iterator() -> AsyncIterator[bytes]:
            total = 0
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    async with client.stream("GET", url, headers=headers) as response:
                        if response.status_code == 404:
                            raise AttachmentProviderError(
                                "Attachment provider object was not found"
                            )
                        response.raise_for_status()
                        declared = response.headers.get("content-length")
                        if declared is not None:
                            try:
                                declared_size = int(declared)
                            except ValueError as error:
                                raise AttachmentProviderError(
                                    "Attachment provider returned invalid length"
                                ) from error
                            if declared_size > max_size_bytes:
                                raise AttachmentProviderError(
                                    "Attachment exceeds the configured size limit"
                                )
                        async for chunk in response.aiter_bytes(64 * 1024):
                            if not chunk:
                                continue
                            total += len(chunk)
                            if total > max_size_bytes:
                                raise AttachmentProviderError(
                                    "Attachment exceeds the configured size limit"
                                )
                            yield chunk
            except AttachmentProviderError:
                raise
            except httpx.HTTPStatusError as error:
                raise AttachmentProviderError(
                    f"Attachment provider HTTP {error.response.status_code}"
                ) from error
            except httpx.HTTPError as error:
                raise AttachmentProviderError(
                    "Attachment provider transport failed"
                ) from error

        return iterator()


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
        message_processor: MessageProcessor,
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
        return (
            f"{envelope.client_type.value}:conversation:{conversation}{suffix}"
        )

    async def submit_envelope(
        self,
        envelope: ClientInputEnvelope,
        *,
        upload_streams: Mapping[str, AsyncIterator[bytes]] | None = None,
    ) -> InputSubmissionResult:
        streams = dict(upload_streams or {})
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
            streams[slot.slot_id] = await provider.open_stream(
                locator.locator,
                max_size_bytes=self.api.artifact_config.max_artifact_size_bytes,
            )

        grouping = resolve_input_grouping(envelope)
        try:
            return await self.api.ingress_services.ingress_service.submit_atomic(
                envelope,
                session_id=self.session_id_for(envelope),
                upload_streams=streams,
                grouping_mode=grouping.mode,
                grouping_key=grouping.key,
            )
        except AttachmentProviderError:
            await self._mark_provider_stream_failed(envelope)
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

    async def commit_grouped_batch(
        self,
        input_batch_id: str,
        *,
        session_id: str,
    ) -> tuple[CommittedInputBatch, bool]:
        """Commit one complete grouped draft; repeated calls are idempotent."""
        store = self.api.ingress_services.batch_store
        draft = await store.get_draft(input_batch_id)
        if draft.session_id != session_id:
            raise ArtifactAccessError("Input batch belongs to another session")
        if draft.grouping_mode == InputGroupingMode.ATOMIC:
            raise IngressConflictError(
                "Atomic input batches are committed during submission"
            )

        try:
            committed = await store.get_committed(input_batch_id)
        except IngressNotFoundError:
            pass
        else:
            return committed, True

        commit_batch = getattr(store, "commit_batch", None)
        if commit_batch is None:
            raise IngressConflictError(
                "Grouped input batch store is not configured"
            )
        committed, duplicate = await commit_batch(
            input_batch_id,
            session_id=session_id,
            reason="explicit_client_commit",
        )
        return committed, duplicate

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
        return await self.message_processor.process_committed_batch(
            batch,
            progress_callback=progress_callback,
            progress_locale=progress_locale,
        )

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
        return record.public_ref()

    async def claim_delivery(
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
        return await self.api.artifact_services.delivery_service.claim(
            delivery_id
        )

    async def open_delivery(
        self,
        delivery_id: str,
        *,
        session_id: str,
        client_type: ClientType,
    ) -> AsyncIterator[bytes]:
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
