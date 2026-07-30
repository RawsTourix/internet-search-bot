"""Authenticated HTTP controls for presentation generation relocation."""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..interaction.presentation import PresentationDeletionState


class InputPresentationRelocateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    presentation_token: str
    client_message_id: str
    expected_generation: int = Field(ge=1)


class InputPresentationDeletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    presentation_token: str
    generation: int = Field(ge=1)
    deletion_state: PresentationDeletionState


def create_presentation_relocation_router(
    *,
    api,
    auth_dependency,
    api_key_scopes: Mapping[str, frozenset[str]],
) -> APIRouter:
    """Create shared relocation routes with transport and token authority."""

    router = APIRouter(prefix="/internal/input-presentations")
    presentation_store = api.ingress_services.presentation_store
    batch_store = api.ingress_services.batch_store

    def require_transport_scope(api_key: str, client_binding_id: str) -> None:
        transport = client_binding_id.split(":", 1)[0].strip().lower()
        scopes = api_key_scopes.get(api_key, frozenset())
        if "*" not in scopes and transport not in scopes:
            raise HTTPException(
                status_code=403,
                detail="API key is not authorized for this presentation transport",
            )

    async def authorize(
        presentation_id: str,
        *,
        session_id: str,
        api_key: str,
    ):
        presentation = await presentation_store.get(presentation_id)
        require_transport_scope(api_key, presentation.client_binding_id)
        draft = await batch_store.get_draft(presentation.input_batch_id)
        if draft.session_id != session_id:
            raise HTTPException(
                status_code=403,
                detail="Input presentation authority mismatch",
            )
        return presentation

    @router.post("/{presentation_id}/relocate")
    async def relocate_presentation(
        presentation_id: str,
        body: InputPresentationRelocateRequest,
        api_key: str = Depends(auth_dependency),
    ):
        await authorize(
            presentation_id,
            session_id=body.session_id,
            api_key=api_key,
        )
        updated = await presentation_store.bind_relocation(
            presentation_id,
            client_message_id=body.client_message_id,
            token=body.presentation_token,
            expected_generation=body.expected_generation,
        )
        return updated.model_dump(mode="json")

    @router.post("/{presentation_id}/superseded-deletion")
    async def record_superseded_deletion(
        presentation_id: str,
        body: InputPresentationDeletionRequest,
        api_key: str = Depends(auth_dependency),
    ):
        await authorize(
            presentation_id,
            session_id=body.session_id,
            api_key=api_key,
        )
        updated = await presentation_store.record_superseded_deletion(
            presentation_id,
            generation=body.generation,
            state=body.deletion_state,
            token=body.presentation_token,
        )
        return updated.model_dump(mode="json")

    return router
