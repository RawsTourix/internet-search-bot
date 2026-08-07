"""Internal transport-worker routes for durable semantic AgentEmission delivery."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from ..input_runtime.composition import get_input_runtime_binding
from ..input_runtime.emissions import AgentEmissionDeliveryReceipt
from ..input_runtime.errors import (
    InputRuntimeConflictError,
    InputRuntimeError,
    InputRuntimeNotFoundError,
)


class EmissionTransportAuthorityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    client_type: str
    client_instance_id: str


class EmissionTransportClaimRequest(EmissionTransportAuthorityRequest):
    claim_token: str


class EmissionTransportReceiptRequest(EmissionTransportAuthorityRequest):
    cycle_id: str
    generation: int
    claim_token: str
    outcome: str
    attempt_number: int
    conversation_id: str
    thread_id: str | None = None
    external_message_id: str | None = None
    delivered_at: datetime | None = None
    error_code: str | None = None


TransportInstanceScopes = Mapping[str, frozenset[tuple[str, str]]]


def add_emission_outbox_routes(
    router: APIRouter,
    *,
    auth_dependency,
    api_key_scopes: Mapping[str, frozenset[str]],
    api_key_instance_scopes: TransportInstanceScopes | None = None,
) -> None:
    """Attach route-scoped READY/claim/receipt endpoints to an existing router."""

    def binding():
        value = get_input_runtime_binding()
        if value is None or not value.config.enabled:
            raise HTTPException(status_code=503, detail="input runtime unavailable")
        return value

    def require_transport_scope(
        api_key: str,
        client_type: str,
        client_instance_id: str,
    ) -> tuple[str, str]:
        normalized_client = client_type.strip().lower()
        normalized_instance = client_instance_id.strip()
        if not normalized_client or not normalized_instance:
            raise HTTPException(
                status_code=422,
                detail="client_type and client_instance_id must not be empty",
            )
        scopes = api_key_scopes.get(api_key, frozenset())
        if "*" not in scopes and normalized_client not in scopes:
            raise HTTPException(
                status_code=403,
                detail="API key is not authorized for this emission transport",
            )
        if api_key_instance_scopes is not None:
            instance_scopes = api_key_instance_scopes.get(api_key, frozenset())
            accepted = {
                ("*", "*"),
                (normalized_client, "*"),
                (normalized_client, normalized_instance),
            }
            if not accepted.intersection(instance_scopes):
                raise HTTPException(
                    status_code=403,
                    detail="API key is not authorized for this client instance",
                )
        return normalized_client, normalized_instance

    @router.get("/internal/emission-outbox/ready")
    async def list_ready_emissions(
        client_type: str,
        client_instance_id: str,
        limit: int = Query(default=50, ge=1, le=200),
        api_key: str = Depends(auth_dependency),
    ):
        normalized_client, normalized_instance = require_transport_scope(
            api_key,
            client_type,
            client_instance_id,
        )
        try:
            rows = await binding().emission_outbox_service.list_ready(
                client_type=normalized_client,
                client_instance_id=normalized_instance,
                limit=limit,
            )
            return {
                "emissions": [row.model_dump(mode="json") for row in rows]
            }
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except InputRuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @router.post("/internal/emission-outbox/{emission_id}/claim")
    async def claim_ready_emission(
        emission_id: str,
        body: EmissionTransportClaimRequest,
        api_key: str = Depends(auth_dependency),
    ):
        normalized_client, normalized_instance = require_transport_scope(
            api_key,
            body.client_type,
            body.client_instance_id,
        )
        try:
            claimed = await binding().emission_outbox_service.claim(
                emission_id,
                session_id=body.session_id,
                client_type=normalized_client,
                client_instance_id=normalized_instance,
                claim_token=body.claim_token,
            )
            return {"emission": claimed.model_dump(mode="json")}
        except InputRuntimeNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except InputRuntimeConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except InputRuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @router.post("/internal/emission-outbox/{emission_id}/receipt")
    async def persist_emission_receipt(
        emission_id: str,
        body: EmissionTransportReceiptRequest,
        api_key: str = Depends(auth_dependency),
    ):
        normalized_client, normalized_instance = require_transport_scope(
            api_key,
            body.client_type,
            body.client_instance_id,
        )
        service = binding().emission_outbox_service
        outcome = body.outcome.strip().lower()
        try:
            if outcome == "delivered":
                if body.external_message_id is None or body.delivered_at is None:
                    raise ValueError(
                        "delivered receipt requires external_message_id and delivered_at"
                    )
                receipt = AgentEmissionDeliveryReceipt(
                    emission_id=emission_id,
                    session_id=body.session_id,
                    cycle_id=body.cycle_id,
                    generation=body.generation,
                    claim_token=body.claim_token,
                    attempt_number=body.attempt_number,
                    client_type=normalized_client,
                    client_instance_id=normalized_instance,
                    conversation_id=body.conversation_id,
                    thread_id=body.thread_id,
                    external_message_id=body.external_message_id,
                    delivered_at=body.delivered_at,
                )
                updated = await service.repository.record_delivery_receipt(receipt)
            elif outcome in {"failed", "unknown"}:
                if not body.error_code:
                    raise ValueError("failure receipt requires error_code")
                updated = await service.failed(
                    emission_id,
                    claim_token=body.claim_token,
                    error_code=body.error_code,
                    ambiguous=(outcome == "unknown"),
                )
            else:
                raise ValueError("unsupported emission delivery outcome")
            return {"emission": updated.model_dump(mode="json")}
        except InputRuntimeNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=str(error)) from error
        except InputRuntimeConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except InputRuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
