"""Structured IR-9 HTTP diagnostics routes.

Web credentials are scoped to the existing Web session namespace. Only an
existing internal ``*`` credential may address an explicit internal session ID.
"""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import Depends, HTTPException, Query

from ..core.models import ClientType
from ..core.session_ids import resolve_message_session_id
from ..input_runtime.diagnostics import RuntimeDiagnosticsError


def add_runtime_diagnostics_routes(
    router,
    *,
    facade,
    auth_dependency,
    api_key_scopes: Mapping[str, frozenset[str]],
) -> None:
    service = facade.api.input_runtime_diagnostics

    def resolve_session(api_key: str, session_id: str) -> str:
        normalized = str(session_id).strip()
        if not normalized:
            raise HTTPException(status_code=422, detail="invalid_session_id")
        scopes = api_key_scopes.get(api_key, frozenset())
        if "*" in scopes:
            return normalized
        if "web" not in scopes:
            raise HTTPException(
                status_code=403,
                detail="runtime_diagnostics_scope_forbidden",
            )
        # The current Web auth model delegates one opaque Web session token to
        # the authenticated client. Do not let that shared key inject a
        # Telegram/internal namespace through this diagnostics route.
        if ":" in normalized:
            raise HTTPException(
                status_code=403,
                detail="runtime_diagnostics_session_forbidden",
            )
        return resolve_message_session_id(
            client_type=ClientType.WEB,
            metadata={"session_id": normalized},
            user_id="runtime-diagnostics",
        )

    def public_error(error: RuntimeDiagnosticsError) -> HTTPException:
        status = 422 if error.reason_code.startswith("invalid_") else 503
        if error.reason_code == "runtime_not_ready":
            status = 409
        return HTTPException(status_code=status, detail=error.reason_code)

    @router.get("/runtime/status")
    async def runtime_status(
        session_id: str,
        api_key: str = Depends(auth_dependency),
    ):
        authoritative_session = resolve_session(api_key, session_id)
        try:
            snapshot = await service.status(authoritative_session)
            return snapshot.model_dump(mode="json")
        except RuntimeDiagnosticsError as error:
            raise public_error(error) from error

    @router.get("/runtime/timeline")
    async def runtime_timeline(
        session_id: str,
        limit: int = Query(
            default=service.DEFAULT_TIMELINE_LIMIT,
            ge=1,
            le=service.MAX_TIMELINE_LIMIT,
        ),
        api_key: str = Depends(auth_dependency),
    ):
        authoritative_session = resolve_session(api_key, session_id)
        try:
            timeline = await service.timeline(
                authoritative_session,
                limit=limit,
            )
            return timeline.model_dump(mode="json")
        except RuntimeDiagnosticsError as error:
            raise public_error(error) from error
