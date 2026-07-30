"""Central HTTP registration for stable Gateway domain contracts."""

from __future__ import annotations

import logging
import os

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from pydantic import ValidationError

from ..artifacts import (
    ArtifactAccessError,
    ArtifactDeliveryError,
    ArtifactDeliveryNotFoundError,
    ArtifactIntegrityError,
    ArtifactStorageError,
    ArtifactValidationError,
)
from ..ingress import (
    IngressConflictError,
    IngressNotFoundError,
    IngressValidationError,
)
from ..interaction.errors import (
    InteractionIntegrityError,
    InteractionStorageError,
    InteractionValidationError,
    OutputBatchConflictError,
    OutputBatchNotFoundError,
    PresentationConflictError,
    PresentationNotFoundError,
)


logger = logging.getLogger("Gateway.DomainErrors")
_DOMAIN_API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


def _response(status_code: int, error: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "detail": str(error),
            "error_type": type(error).__name__,
        },
    )


def _configured_transport_authority():
    scopes: dict[str, set[str]] = {}
    instances: dict[str, set[tuple[str, str]]] = {}
    telegram_instance = (
        os.getenv("TELEGRAM_BOT_INSTANCE_ID", "default").strip()
        or "default"
    )
    for environment_name, scope, instance_scope in (
        (
            "TELEGRAM_API_KEY",
            "telegram",
            ("telegram", telegram_instance),
        ),
        ("WEB_API_KEY", "web", ("web", "*")),
        ("INTERNAL_API_KEY", "*", ("*", "*")),
    ):
        value = os.getenv(environment_name, "").strip()
        if not value:
            continue
        scopes.setdefault(value, set()).add(scope)
        instances.setdefault(value, set()).add(instance_scope)
    return (
        {key: frozenset(value) for key, value in scopes.items()},
        {key: frozenset(value) for key, value in instances.items()},
    )


async def _domain_api_key_auth(
    api_key: str | None = Depends(_DOMAIN_API_KEY_HEADER),
) -> str:
    scopes, _ = _configured_transport_authority()
    if not api_key:
        raise HTTPException(status_code=401, detail="Missing API Key")
    if api_key not in scopes:
        raise HTTPException(status_code=403, detail="Invalid API Key")
    return api_key


def _register_input_collection_routes(app: FastAPI) -> None:
    if getattr(app.state, "input_collection_routes_registered", False):
        return
    scopes, instance_scopes = _configured_transport_authority()
    if not scopes:
        logger.debug(
            "Input collection routes skipped: no transport API keys configured"
        )
        return

    # Imported lazily to keep exception models independent from the global API
    # composition root during unit-test module import.
    from .api import API
    from .input_collection_routes import create_input_collection_router

    app.include_router(
        create_input_collection_router(
            api=API,
            auth_dependency=_domain_api_key_auth,
            api_key_scopes=scopes,
            api_key_instance_scopes=instance_scopes,
        )
    )
    app.state.input_collection_routes_registered = True


def register_domain_exception_handlers(app: FastAPI) -> None:
    """Register authoritative HTTP error policy and shared domain routes."""

    not_found = (
        ArtifactDeliveryNotFoundError,
        IngressNotFoundError,
        OutputBatchNotFoundError,
        PresentationNotFoundError,
    )
    conflicts = (
        ArtifactDeliveryError,
        IngressConflictError,
        OutputBatchConflictError,
        PresentationConflictError,
    )
    validation = (
        ArtifactValidationError,
        IngressValidationError,
        InteractionValidationError,
        ValidationError,
    )
    unavailable = (
        ArtifactIntegrityError,
        ArtifactStorageError,
        InteractionIntegrityError,
        InteractionStorageError,
    )

    for error_type in not_found:
        async def handle_not_found(
            request: Request,
            error: Exception,
            *,
            _status: int = status.HTTP_404_NOT_FOUND,
        ) -> JSONResponse:
            del request
            return _response(_status, error)

        app.add_exception_handler(error_type, handle_not_found)

    async def handle_access(request: Request, error: Exception) -> JSONResponse:
        del request
        return _response(status.HTTP_403_FORBIDDEN, error)

    app.add_exception_handler(ArtifactAccessError, handle_access)

    for error_type in conflicts:
        async def handle_conflict(
            request: Request,
            error: Exception,
            *,
            _status: int = status.HTTP_409_CONFLICT,
        ) -> JSONResponse:
            del request
            return _response(_status, error)

        app.add_exception_handler(error_type, handle_conflict)

    for error_type in validation:
        async def handle_validation(
            request: Request,
            error: Exception,
            *,
            _status: int = status.HTTP_422_UNPROCESSABLE_CONTENT,
        ) -> JSONResponse:
            del request
            return _response(_status, error)

        app.add_exception_handler(error_type, handle_validation)

    for error_type in unavailable:
        async def handle_unavailable(
            request: Request,
            error: Exception,
            *,
            _status: int = status.HTTP_503_SERVICE_UNAVAILABLE,
        ) -> JSONResponse:
            logger.error(
                "Gateway domain storage unavailable path=%s error_type=%s error=%s",
                request.url.path,
                type(error).__name__,
                error,
            )
            return _response(_status, error)

        app.add_exception_handler(error_type, handle_unavailable)

    _register_input_collection_routes(app)
