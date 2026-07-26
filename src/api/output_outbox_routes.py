"""Internal transport-worker routes for safe READY OutputBatch delivery."""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from ..interaction.errors import (
    InteractionIntegrityError,
    InteractionStorageError,
    InteractionValidationError,
    OutputBatchConflictError,
    OutputBatchNotFoundError,
)
from ..interaction.output_models import OutputBatchKind, OutputDeliveryReceipt
from ..interaction.output_outbox import ReadyOutputOutboxService


class OutputTransportAuthorityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    client_type: str
    client_instance_id: str


class OutputTransportReceiptRequest(OutputTransportAuthorityRequest):
    receipt: OutputDeliveryReceipt


def create_output_outbox_router(
    *,
    facade,
    auth_dependency,
    api_key_scopes: Mapping[str, frozenset[str]],
) -> APIRouter:
    """Create the internal process-local outbox worker API.

    Only sufficiently old READY final batches are listed. DELIVERING and
    UNKNOWN batches are deliberately excluded because their transport outcome
    may be ambiguous. The authenticated key must also own the requested client
    transport; body fields alone never grant authority.
    """

    router = APIRouter()
    service = ReadyOutputOutboxService(facade.api.output_store)

    def require_transport_scope(api_key: str, client_type: str) -> None:
        normalized = client_type.strip().lower()
        scopes = api_key_scopes.get(api_key, frozenset())
        if "*" not in scopes and normalized not in scopes:
            raise HTTPException(
                status_code=403,
                detail="API key is not authorized for this output transport",
            )

    @router.get("/internal/output-outbox/ready")
    async def list_ready_output_batches(
        client_type: str,
        client_instance_id: str,
        limit: int = Query(default=50, ge=1, le=service.MAX_LIMIT),
        minimum_age_seconds: float = Query(
            default=30.0,
            ge=0,
            le=service.MAX_MINIMUM_AGE_SECONDS,
        ),
        api_key: str = Depends(auth_dependency),
    ):
        require_transport_scope(api_key, client_type)
        try:
            batches = await service.list_ready(
                client_type=client_type,
                client_instance_id=client_instance_id,
                kind=OutputBatchKind.FINAL,
                limit=limit,
                minimum_age_seconds=minimum_age_seconds,
            )
            return {
                "output_batches": [
                    batch.model_dump(mode="json") for batch in batches
                ]
            }
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except (InteractionIntegrityError, InteractionStorageError) as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @router.post("/internal/output-outbox/{output_batch_id}/claim")
    async def claim_ready_output_batch(
        output_batch_id: str,
        body: OutputTransportAuthorityRequest,
        api_key: str = Depends(auth_dependency),
    ):
        require_transport_scope(api_key, body.client_type)
        try:
            batch = await facade.api.output_store.get(output_batch_id)
            service.validate_authority(
                batch,
                session_id=body.session_id,
                client_type=body.client_type,
                client_instance_id=body.client_instance_id,
            )
            claimed, attempt_id = await facade.api.output_store.claim_delivery(
                output_batch_id
            )
            plan = facade.api.output_renderer.plan(claimed)
            return {
                "output_batch": claimed.model_dump(mode="json"),
                "attempt_id": attempt_id,
                "delivery_plan": plan.model_dump(mode="json"),
            }
        except OutputBatchNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except OutputBatchConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (InteractionValidationError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except (InteractionIntegrityError, InteractionStorageError) as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @router.post("/internal/output-outbox/{output_batch_id}/receipt")
    async def complete_ready_output_batch(
        output_batch_id: str,
        body: OutputTransportReceiptRequest,
        api_key: str = Depends(auth_dependency),
    ):
        require_transport_scope(api_key, body.client_type)
        try:
            batch = await facade.api.output_store.get(output_batch_id)
            service.validate_authority(
                batch,
                session_id=body.session_id,
                client_type=body.client_type,
                client_instance_id=body.client_instance_id,
            )
            if body.receipt.output_batch_id != output_batch_id:
                raise OutputBatchConflictError(
                    "Output receipt identity mismatch"
                )
            completed = await facade.api.output_completion.complete(
                body.receipt
            )
            return completed.model_dump(mode="json")
        except OutputBatchNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except OutputBatchConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (InteractionValidationError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except (InteractionIntegrityError, InteractionStorageError) as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    return router
