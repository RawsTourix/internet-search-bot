"""Authenticated shared HTTP controls for explicit InputBatch collection."""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from ..artifacts.errors import ArtifactIntegrityError, ArtifactStorageError
from ..core.models import ClientType
from ..ingress import (
    ClientConversationRef,
    ClientResponseRoute,
    InputDraftControlConflictError,
    InputDraftScope,
    IngressConflictError,
    IngressNotFoundError,
)


TransportInstanceScopes = Mapping[
    str,
    frozenset[tuple[str, str]],
]


class InputCollectionScopeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    client_type: ClientType
    client_instance_id: str
    conversation_id: str
    thread_id: str | None = None
    principal_id: str

    def scope(self) -> InputDraftScope:
        return InputDraftScope(
            session_id=self.session_id,
            client_type=self.client_type,
            client_instance_id=self.client_instance_id,
            conversation=ClientConversationRef(
                conversation_id=self.conversation_id,
                thread_id=self.thread_id,
            ),
            principal_id=self.principal_id,
        )


class InputCollectionStartRequest(InputCollectionScopeRequest):
    idempotency_key: str
    locale: str | None = None
    response_route: ClientResponseRoute


class InputCollectionMutationRequest(InputCollectionScopeRequest):
    idempotency_key: str


def create_input_collection_router(
    *,
    api,
    auth_dependency,
    api_key_scopes: Mapping[str, frozenset[str]],
    api_key_instance_scopes: TransportInstanceScopes | None = None,
) -> APIRouter:
    """Create the shared explicit-collection control API.

    Request fields identify the desired scope but do not grant authority. The
    authenticated API key must own the requested client transport and exact
    client instance. Mutating actions remain idempotent through their explicit
    idempotency keys.

    Starting collection is a packaging action. It deliberately does not clear a
    suspended ``WAITING_USER`` AgentCycle: the committed package may be the
    user's multi-message or multi-file continuation of that same task.
    """

    router = APIRouter(prefix="/internal/input-collections")
    service = api.ingress_services.draft_control_service
    if service is None:
        raise RuntimeError("Input draft control service is unavailable")

    def require_transport_scope(
        api_key: str,
        client_type: ClientType,
        client_instance_id: str,
    ) -> None:
        normalized_client = client_type.value
        normalized_instance = client_instance_id.strip()
        if not normalized_instance:
            raise HTTPException(
                status_code=422,
                detail="client_instance_id must not be empty",
            )
        scopes = api_key_scopes.get(api_key, frozenset())
        if "*" not in scopes and normalized_client not in scopes:
            raise HTTPException(
                status_code=403,
                detail="API key is not authorized for this input transport",
            )
        if api_key_instance_scopes is None:
            return
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

    def ensure_route_matches_scope(body: InputCollectionStartRequest) -> None:
        route = body.response_route
        if (
            route.conversation_id != body.conversation_id
            or route.thread_id != body.thread_id
        ):
            raise HTTPException(
                status_code=422,
                detail="response_route must match the exact collection scope",
            )

    def payload(result):
        return result.model_dump(mode="json")

    @router.post("/start")
    async def start_collection(
        body: InputCollectionStartRequest,
        api_key: str = Depends(auth_dependency),
    ):
        require_transport_scope(
            api_key,
            body.client_type,
            body.client_instance_id,
        )
        ensure_route_matches_scope(body)
        try:
            result = await service.start_collection(
                body.scope(),
                response_route=body.response_route,
                locale=body.locale,
                idempotency_key=body.idempotency_key,
            )
            return payload(result)
        except InputDraftControlConflictError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except (ArtifactIntegrityError, ArtifactStorageError) as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @router.post("/inspect")
    async def inspect_collection(
        body: InputCollectionScopeRequest,
        api_key: str = Depends(auth_dependency),
    ):
        require_transport_scope(
            api_key,
            body.client_type,
            body.client_instance_id,
        )
        try:
            result = await service.inspect(body.scope())
            return payload(result)
        except (ArtifactIntegrityError, ArtifactStorageError) as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @router.post("/send")
    async def send_collection(
        body: InputCollectionMutationRequest,
        api_key: str = Depends(auth_dependency),
    ):
        require_transport_scope(
            api_key,
            body.client_type,
            body.client_instance_id,
        )
        try:
            result = await service.commit(
                body.scope(),
                idempotency_key=body.idempotency_key,
            )
            return payload(result)
        except (InputDraftControlConflictError, IngressConflictError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except IngressNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except (ArtifactIntegrityError, ArtifactStorageError) as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @router.post("/cancel")
    async def cancel_collection(
        body: InputCollectionMutationRequest,
        api_key: str = Depends(auth_dependency),
    ):
        require_transport_scope(
            api_key,
            body.client_type,
            body.client_instance_id,
        )
        try:
            result = await service.cancel(
                body.scope(),
                idempotency_key=body.idempotency_key,
            )
            return payload(result)
        except (InputDraftControlConflictError, IngressConflictError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except IngressNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except (ArtifactIntegrityError, ArtifactStorageError) as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    return router
