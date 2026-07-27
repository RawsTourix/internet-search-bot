"""Prevent transports from bypassing exact OutputBatch delivery authority."""

from __future__ import annotations

import hmac
import os
import re

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


_LEGACY_CONTENT_PATH = re.compile(
    r"^/internal/deliveries/[^/]+/content$"
)
_LEGACY_OUTPUT_BATCH_PATH = re.compile(
    r"^/internal/output-batches/[^/]+(?:/(?:claim|receipt|reconcile))?$"
)


class LegacyTelegramDeliveryGuardMiddleware(BaseHTTPMiddleware):
    """Keep transport adapters on instance-scoped OutputBatch routes.

    Legacy aggregate routes have no client-instance contract and therefore are
    reserved for the explicit internal credential. Compatibility byte streaming
    remains available to non-Telegram transports until they migrate, while
    Telegram bytes always require an exact claimed OutputBatch member.
    """

    def __init__(self, app, *, internal_api_key: str | None = None) -> None:
        super().__init__(app)
        self.internal_api_key = (
            str(internal_api_key).strip()
            if internal_api_key is not None
            else os.getenv("INTERNAL_API_KEY", "").strip()
        )

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if (
            request.method == "GET"
            and _LEGACY_CONTENT_PATH.fullmatch(path)
            and request.query_params.get("client_type", "").strip().lower()
            == "telegram"
        ):
            return self._conflict(
                "Telegram delivery content requires an exact "
                "instance-owned OutputBatch claim"
            )
        if _LEGACY_OUTPUT_BATCH_PATH.fullmatch(path):
            supplied = request.headers.get("X-API-Key", "")
            if (
                not self.internal_api_key
                or not supplied
                or not hmac.compare_digest(supplied, self.internal_api_key)
            ):
                return self._conflict(
                    "Transport output delivery requires the instance-scoped "
                    "OutputBatch outbox API"
                )
        return await call_next(request)

    @staticmethod
    def _conflict(detail: str) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"detail": detail},
        )
