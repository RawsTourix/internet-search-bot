"""FastAPI routes for committed input batches and durable artifact delivery."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
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
    CommitGroupedBatchRequest,
    DeliveryFailureRequest,
    DeliveryReceiptRequest,
    RunCommittedBatchRequest,
)
from ..interaction.output_models import OutputDeliveryReceipt
from ..interaction.errors import (
    InteractionIntegrityError,
    InteractionStorageError,
    OutputBatchConflictError,
    OutputBatchNotFoundError,
    PresentationConflictError,
    PresentationNotFoundError,
)


class OutputBatchClaimRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str


class OutputBatchReceiptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    receipt: OutputDeliveryReceipt


class InputPresentationBindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str
    presentation_token: str
    client_message_id: str


class RunCommittedBatchWithProgressRequest(RunCommittedBatchRequest):
    """One execution request with a non-persisted presentation overlay."""

    progress_metadata: dict[str, Any] = Field(default_factory=dict)


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


def _committed_batch_payload(batch) -> dict[str, Any]:
    return {
        "input_batch_id": batch.input_batch_id,
        "session_id": batch.session_id,
        "sequence_number": batch.sequence_number,
        "artifact_count": len(batch.artifact_refs),
        "text_part_count": len(batch.text_parts),
        "committed_at": batch.committed_at.isoformat(),
    }


def _submission_payload(
    result,
    response=None,
    *,
    run_skipped_duplicate: bool = False,
) -> dict[str, Any]:
    payload = {
        "status": result.state,
        "event_id": result.event_id,
        "input_batch_id": result.input_batch_id,
        "duplicate": result.duplicate,
        "error_code": result.error_code,
        "run_skipped_duplicate": run_skipped_duplicate,
        "ack_policy": result.ack_policy.value,
        "presentation_event": (
            result.presentation_event.model_dump(mode="json")
            if result.presentation_event is not None
            else None
        ),
        "presentation_ref": (
            result.presentation_ref.model_dump(mode="json")
            if result.presentation_ref is not None
            else None
        ),
        "response_anchor": (
            result.response_anchor.model_dump(mode="json")
            if result.response_anchor is not None
            else None
        ),
        "counts": {
            "file_count": result.file_count,
            "text_part_count": result.text_part_count,
        },
    }
    if result.committed_batch is not None:
        payload["committed_batch"] = _committed_batch_payload(
            result.committed_batch
        )
    if response is not None:
        payload["response"] = response.content
        payload["metadata"] = response.metadata
    return payload


def _with_run_progress_metadata(batch, metadata: dict[str, Any] | None):
    """Return an in-memory route overlay without mutating durable InputBatch."""

    overlay = dict(metadata or {})
    if not overlay:
        return batch
    route = batch.response_route
    merged = dict(route.metadata or {})
    merged.update(overlay)
    return batch.model_copy(update={
        "response_route": route.model_copy(update={"metadata": merged})
    })


def create_artifact_router(
    *,
    facade: ArtifactTransportFacade,
    auth_dependency,
    progress_callback_factory: ProgressCallbackFactory | None = None,
) -> APIRouter:
    router = APIRouter()

    async def run_batch(
        batch,
        *,
        progress_locale: str,
        progress_metadata: dict[str, Any] | None = None,
    ):
        presentation_batch = _with_run_progress_metadata(
            batch,
            progress_metadata,
        )
        callback = (
            progress_callback_factory(presentation_batch)
            if progress_callback_factory is not None
            else None
        )
        return await facade.run_committed_batch(
            batch.input_batch_id,
            session_id=batch.session_id,
            progress_callback=callback,
            progress_locale=progress_locale,
        )

    async def run_submission(result, *, run: bool, progress_locale: str):
        if not run or result.committed_batch is None or result.duplicate:
            return None
        return await run_batch(
            result.committed_batch,
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
            response = await run_submission(
                result,
                run=run,
                progress_locale=progress_locale,
            )
            code = (
                status.HTTP_201_CREATED
                if result.state == "committed"
                else status.HTTP_202_ACCEPTED
            )
            if result.duplicate:
                code = status.HTTP_200_OK
            if result.state == "failed":
                code = status.HTTP_422_UNPROCESSABLE_ENTITY
            return JSONResponse(
                status_code=code,
                content=_submission_payload(
                    result,
                    response,
                    run_skipped_duplicate=bool(
                        run and result.duplicate and result.committed_batch is not None
                    ),
                ),
            )
        except IngressConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (IngressValidationError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except AttachmentProviderError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error
        except (ArtifactStorageError, ArtifactIntegrityError) as error:
            raise HTTPException(
                status_code=503,
                detail="Ingress storage unavailable",
            ) from error

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
                if not isinstance(value, UploadFile):
                    continue
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
            response = await run_submission(
                result,
                run=run,
                progress_locale=progress_locale,
            )
            code = (
                status.HTTP_201_CREATED
                if result.state == "committed"
                else status.HTTP_202_ACCEPTED
            )
            if result.duplicate:
                code = status.HTTP_200_OK
            if result.state == "failed":
                code = status.HTTP_422_UNPROCESSABLE_ENTITY
            return JSONResponse(
                status_code=code,
                content=_submission_payload(
                    result,
                    response,
                    run_skipped_duplicate=bool(
                        run and result.duplicate and result.committed_batch is not None
                    ),
                ),
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
            raise HTTPException(
                status_code=503,
                detail="Ingress storage unavailable",
            ) from error

    @router.post(
        "/input-batches/{input_batch_id}/commit",
        dependencies=[Depends(auth_dependency)],
    )
    async def commit_input_batch(
        input_batch_id: str,
        body: CommitGroupedBatchRequest,
    ):
        try:
            commit_with_presentation = getattr(
                facade,
                "commit_grouped_batch_with_presentation",
                None,
            )
            presentation_result = None
            if commit_with_presentation is None:
                batch, duplicate = await facade.commit_grouped_batch(
                    input_batch_id,
                    session_id=body.session_id,
                )
            else:
                batch, duplicate, presentation_result = (
                    await commit_with_presentation(
                        input_batch_id,
                        session_id=body.session_id,
                    )
                )
            response = None
            if body.run and not duplicate:
                response = await run_batch(
                    batch,
                    progress_locale=body.progress_locale,
                )
            payload: dict[str, Any] = {
                "status": "committed",
                "input_batch_id": batch.input_batch_id,
                "duplicate": duplicate,
                "run_skipped_duplicate": bool(body.run and duplicate),
                "committed_batch": _committed_batch_payload(batch),
            }
            if presentation_result is not None:
                ack_policy, presentation_event, presentation_ref = (
                    presentation_result
                )
                payload.update(
                    {
                        "ack_policy": ack_policy.value,
                        "presentation_event": presentation_event.model_dump(
                            mode="json"
                        ),
                        "presentation_ref": presentation_ref.model_dump(
                            mode="json"
                        ),
                    }
                )
            if response is not None:
                payload["response"] = response.content
                payload["metadata"] = response.metadata
            return JSONResponse(
                status_code=(
                    status.HTTP_200_OK
                    if duplicate
                    else status.HTTP_201_CREATED
                ),
                content=payload,
            )
        except IngressNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ArtifactAccessError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ArtifactDeliveryError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except IngressConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (ArtifactStorageError, ArtifactIntegrityError) as error:
            raise HTTPException(
                status_code=503,
                detail="Ingress storage unavailable",
            ) from error

    @router.post(
        "/input-batches/{input_batch_id}/run",
        dependencies=[Depends(auth_dependency)],
    )
    async def run_input_batch(
        input_batch_id: str,
        body: RunCommittedBatchWithProgressRequest,
    ):
        try:
            batch = await facade.api.ingress_services.batch_store.get_committed(
                input_batch_id
            )
            if batch.session_id != body.session_id:
                raise ArtifactAccessError("Input batch belongs to another session")
            response = await run_batch(
                batch,
                progress_locale=body.progress_locale,
                progress_metadata=body.progress_metadata,
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
                client_type=_client_type(client_type),
            )
            return ref.model_dump(mode="json")
        except ArtifactDeliveryNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ArtifactAccessError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except ArtifactDeliveryError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @router.get(
        "/internal/output-batches/unknown",
        dependencies=[Depends(auth_dependency)],
    )
    async def list_unknown_output_batches(session_id: str):
        batches = await facade.api.output_store.list_unknown()
        return {
            "output_batches": [
                item.model_dump(mode="json")
                for item in batches
                if item.session_id == session_id
            ]
        }

    @router.post(
        "/internal/output-batches/{output_batch_id}/reconcile",
        dependencies=[Depends(auth_dependency)],
    )
    async def reconcile_unknown_output_batch(
        output_batch_id: str,
        body: OutputBatchReceiptRequest,
    ):
        batch = await facade.api.output_store.get(output_batch_id)
        if batch.session_id != body.session_id:
            raise HTTPException(
                status_code=403,
                detail="Output batch authority mismatch",
            )
        if body.receipt.output_batch_id != output_batch_id:
            raise HTTPException(
                status_code=409,
                detail="Output receipt identity mismatch",
            )
        try:
            reconciled = await facade.api.output_store.reconcile_unknown(
                body.receipt
            )
            return reconciled.model_dump(mode="json")
        except OutputBatchConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @router.get(
        "/internal/output-batches/{output_batch_id}",
        dependencies=[Depends(auth_dependency)],
    )
    async def get_output_batch(output_batch_id: str, session_id: str):
        try:
            batch = await facade.api.output_store.get(output_batch_id)
            if batch.session_id != session_id:
                raise HTTPException(
                    status_code=403,
                    detail="Output batch authority mismatch",
                )
            return {
                "output_batch": batch.model_dump(mode="json"),
                "delivery_plan": facade.api.output_renderer.plan(batch).model_dump(
                    mode="json"
                ),
            }
        except OutputBatchNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except (InteractionIntegrityError, InteractionStorageError) as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @router.post(
        "/internal/input-presentations/{presentation_id}/bind",
        dependencies=[Depends(auth_dependency)],
    )
    async def bind_input_presentation(
        presentation_id: str,
        body: InputPresentationBindRequest,
    ):
        try:
            presentation = (
                await facade.api.ingress_services.presentation_store.get(
                    presentation_id
                )
            )
            draft = await facade.api.ingress_services.batch_store.get_draft(
                presentation.input_batch_id
            )
            if draft.session_id != body.session_id:
                raise HTTPException(
                    status_code=403,
                    detail="Input presentation authority mismatch",
                )
            bound = await facade.api.ingress_services.presentation_store.bind(
                presentation_id,
                client_message_id=body.client_message_id,
                token=body.presentation_token,
            )
            return bound.model_dump(mode="json")
        except PresentationNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except IngressNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except PresentationConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (InteractionIntegrityError, InteractionStorageError) as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @router.post(
        "/internal/output-batches/{output_batch_id}/claim",
        dependencies=[Depends(auth_dependency)],
    )
    async def claim_output_batch(
        output_batch_id: str,
        body: OutputBatchClaimRequest,
    ):
        try:
            batch = await facade.api.output_store.get(output_batch_id)
            if batch.session_id != body.session_id:
                raise HTTPException(
                    status_code=403,
                    detail="Output batch authority mismatch",
                )
            claimed, attempt_id = await facade.api.output_store.claim_delivery(
                output_batch_id
            )
            return {
                "output_batch": claimed.model_dump(mode="json"),
                "attempt_id": attempt_id,
                "delivery_plan": facade.api.output_renderer.plan(claimed).model_dump(
                    mode="json"
                ),
            }
        except OutputBatchNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except OutputBatchConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (InteractionIntegrityError, InteractionStorageError) as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @router.post(
        "/internal/output-batches/{output_batch_id}/receipt",
        dependencies=[Depends(auth_dependency)],
    )
    async def complete_output_batch(
        output_batch_id: str,
        body: OutputBatchReceiptRequest,
    ):
        try:
            batch = await facade.api.output_store.get(output_batch_id)
            if batch.session_id != body.session_id:
                raise HTTPException(
                    status_code=403,
                    detail="Output batch authority mismatch",
                )
            if body.receipt.output_batch_id != output_batch_id:
                raise HTTPException(
                    status_code=409,
                    detail="Output receipt identity mismatch",
                )
            completed = await facade.api.output_completion.complete(
                body.receipt
            )
            return completed.model_dump(mode="json")
        except OutputBatchNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except OutputBatchConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (InteractionIntegrityError, InteractionStorageError) as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

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
            raise HTTPException(
                status_code=503,
                detail="Delivery storage unavailable",
            ) from error

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
