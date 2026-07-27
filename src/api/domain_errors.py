"""Central HTTP mapping for stable domain errors exposed by the Gateway."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
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


def _response(status_code: int, error: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "detail": str(error),
            "error_type": type(error).__name__,
        },
    )


def register_domain_exception_handlers(app: FastAPI) -> None:
    """Register one authoritative HTTP policy for shared runtime domains."""

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
