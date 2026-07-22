"""FastAPI routes for committed input batches and durable artifact delivery."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.datastructures import UploadFile

from ..artifacts import (
    ArtifactAccessError,
    ArtifactDeliveryError,
    ArtifactDeliveryNotFoundError,
    ArtifactIntegrityError,
    ArtifactStorageError,
)
from ..ingress import (
    ClientInputEnvelope,
    IngressConflictError,
    IngressNotFoundError,
    IngressValidationError,
)
from .artifact_transport import (
    ArtifactTransportFacade,
    AttachmentProviderError,
    DeliveryFailureRequest,
    DeliveryReceiptRequest,
    RunCommittedBatchRequest,
)


ProgressCallbackFactory = Callable[[Any], Any]


def _upload_stream(upload: UploadFile) -> AsyncIterator[bytes]:
    async def iterator() -> AsyncIterator[bytes]:
        try:
            while True:
                chunk = await upload.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            await upload.close()

    return iterator()


def _submission_payload(result, response=None) -> dict[str, Any]:
    payload = {
        "status": result.state,
        "event_id": result.event_id,
        "input_batch_id": result.input_batch_id,
        "duplicate": result.duplicate,
        "error_code": result.error_code,
    }
    if result.committed_batch is not None:
        payload["committed_batch"] = {
            "input_batch_id": result.committed_batch.input_batch_id,
            "session_id": result.committed_batch.session_id,
            "sequence_number": result.committed_batch.sequence_number,
            "artifact_count": len(result.committed_batch.artifact_refs),
            "text_part_count": len(result.committed_batch.text_parts),
            "committed_at": result.committed_batch.committed_at.isoformat(),
        }
    if response is not None:
        payload["response"] = response.content
        payload["metadata"] = response.metadata
    return payload


def create_artifact_router(
    *,
    facade: ArtifactTransportFacade,
    auth_dependency,
    progress_callback_factory: ProgressCallbackFactory | None = None,
) -> APIRouter:
    router = APIRouter()

    async def _run_if_requested(result, *, run: bool, progress_locale: str):
        if not run or result.committed_batch is None:
            return None
        callback = None
        if progress_callback_factory is not None:
            callback = progress_callback_factory(result.committed_batch)
        return await facade.run_committed_batch(
            result.input_batch_id,
            session_id=result.committed_batch.session_id,
            progress_callback=callback,
            progress_locale=progress_locale,
        )

    @router.post(
        "/ingress/events",
        dependencies=[Depends(auth_dependency)],
    )
    async def submit_ingress_event(
        envelope: ClientInputEnvelope,
        run: bool = False,
        progress_locale: str = "ru",
    ):
        try:
            result = await facade.submit_envelope(envelope)
            response = await _run_if_requested(
                result,
                run=run,
                progress_locale=progress_locale,
            )
            code = (
                status.HTTP_201_CREATED
                if result.state == "committed"
                else status.HTTP_202_ACCEPTED
            )
            if result.state == "failed":
                code = status.HTTP_422_UNPROCESSABLE_ENTITY
            return JSONResponse(
                status_code=code,
                content=_submission_payload(result, response),
            )
        except IngressConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (IngressValidationError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except AttachmentProviderError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        except (ArtifactStorageError, ArtifactIntegrityError) as error:
            raise HTTPException(status_code=503, detail="Ingress storage unavailable") from error

    @router.post(
        "/web/input-batches",
        dependencies=[Depends(auth_dependency)],
    )
    async def submit_web_input_batch(
        request: Request,
        run: bool = False,
        progress_locale: str = "ru",
    ):
        try:
            form = await request.form()
            raw_manifest = form.get("manifest")
            if not isinstance(raw_manifest, str):
                raise HTTPException(
                    status_code=422,
                    detail="Multipart request requires a JSON manifest field",
                )
            envelope = ClientInputEnvelope.model_validate_json(raw_manifest)
            upload_fields: dict[str, UploadFile] = {}
            for field_name, value in form.multi_items():
                if isinstance(value, UploadFile):
                    if field_name in upload_fields:
                        raise HTTPException(
                            status_code=422,
                            detail=f"Duplicate upload field {field_name!r}",
                        )
                    upload_fields[field_name] = value

            streams: dict[str, AsyncIterator[bytes]] = {}
            expected_fields: set[str] = set()
            for slot in envelope.attachment_slots:
                field_name = slot.upload_field_name
                if field_name is None:
                    continue
                expected_fields.add(field_name)
                upload = upload_fields.get(field_name)
                if upload is None:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Missing multipart field {field_name!r}",
                    )
                streams[slot.slot_id] = _upload_stream(upload)
            unexpected = set(upload_fields) - expected_fields
            if unexpected:
                raise HTTPException(
                    status_code=422,
                    detail=f"Unexpected multipart fields: {sorted(unexpected)}",
                )

            result = await facade.submit_envelope(
                envelope,
                upload_streams=streams,
            )
            response = await _run_if_requested(
                result,
                run=run,
                progress_locale=progress_locale,
            )
            code = (
                status.HTTP_201_CREATED
                if result.state == "committed"
                else status.HTTP_202_ACCEPTED
            )
            if result.state == "failed":
                code = status.HTTP_422_UNPROCESSABLE_ENTITY
            return JSONResponse(
                status_code=code,
                content=_submission_payload(result, response),
            )
        except HTTPException:
            raise
        except IngressConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (IngressValidationError, ValueError, json.JSONDecodeError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except AttachmentProviderError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        except (ArtifactStorageError, ArtifactIntegrityError) as error:
            raise HTTPException(status_code=503, detail="Ingress storage unavailable") from error

    @router.post(
        "/input-batches/{input_batch_id}/run",
        dependencies=[Depends(auth_dependency)],
    )
    async def run_input_batch(
        input_batch_id: str,
        body: RunCommittedBatchRequest,
    ):
        try:
            batch = await facade.api.ingress_services.batch_store.get_committed(
                input_batch_id
            )
            callback = (
                progress_callback_factory(batch)
                if progress_callback_factory is not None
                else None
            )
            response = await facade.run_committed_batch(
                input_batch_id,
                session_id=body.session_id,
                progress_callback=callback,
                progress_locale=body.progress_locale,
            )
            return {
                "status": "ok",
                "response": response.content,
                "metadata": response.metadata,
            }
        except IngressNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ArtifactAccessError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error

    @router.get(
        "/internal/deliveries/{delivery_id}",
        dependencies=[Depends(auth_dependency)],
    )
    async def get_delivery(
        delivery_id: str,
        session_id: str,
        client_type: str,
    ):
        try:
            ref = await facade.get_delivery_ref(
                delivery_id,
                session_id=session_id,
                client_type=facade.api.core_client_type(client_type)
                if hasattr(facade.api, "core_client_type")
                else _client_type(client_type),
            )
            return ref.model_dump(mode="json")
        except ArtifactDeliveryNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ArtifactAccessError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error

    @router.get(
        "/internal/deliveries/{delivery_id}/content",
        dependencies=[Depends(auth_dependency)],
    )
    async def stream_delivery(
        delivery_id: str,
        session_id: str,
        client_type: str,
    ):
        try:
            normalized_client = _client_type(client_type)
            ref = await facade.claim_delivery(
                delivery_id,
                session_id=session_id,
                client_type=normalized_client,
            )
            iterator = await facade.open_delivery(
                delivery_id,
                session_id=session_id,
                client_type=normalized_client,
            )
            encoded_filename = quote(ref.filename, safe="")
            return StreamingResponse(
                iterator,
                media_type=ref.mime_type,
                headers={
                    "Content-Length": str(ref.size_bytes),
                    "Content-Disposition": (
                        "attachment; filename*=UTF-8''" + encoded_filename
                    ),
                    "X-Delivery-ID": ref.delivery_id,
                    "X-Content-Hash": ref.content_hash,
                },
            )
        except ArtifactDeliveryNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ArtifactAccessError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ArtifactDeliveryError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (ArtifactStorageError, ArtifactIntegrityError) as error:
            raise HTTPException(status_code=503, detail="Delivery storage unavailable") from error

    @router.post(
        "/internal/deliveries/{delivery_id}/complete",
        dependencies=[Depends(auth_dependency)],
    )
    async def complete_delivery(
        delivery_id: str,
        body: DeliveryReceiptRequest,
    ):
        try:
            ref = await facade.complete_delivery(delivery_id, body)
            return ref.model_dump(mode="json")
        except ArtifactDeliveryNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ArtifactAccessError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ArtifactDeliveryError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @router.post(
        "/internal/deliveries/{delivery_id}/failed",
        dependencies=[Depends(auth_dependency)],
    )
    async def fail_delivery(
        delivery_id: str,
        body: DeliveryFailureRequest,
    ):
        try:
            ref = await facade.fail_delivery(delivery_id, body)
            return ref.model_dump(mode="json")
        except ArtifactDeliveryNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ArtifactAccessError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ArtifactDeliveryError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    return router


def _client_type(value: str):
    from ..core.models import ClientType

    try:
        return ClientType(value.strip().lower())
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Unsupported client_type") from error
