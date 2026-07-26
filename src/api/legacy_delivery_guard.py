"""Prevent scoped transports from bypassing OutputBatch delivery authority."""

from __future__ import annotations

import re

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


_LEGACY_CONTENT_PATH = re.compile(
    r"^/internal/deliveries/[^/]+/content$"
)


class LegacyTelegramDeliveryGuardMiddleware(BaseHTTPMiddleware):
    """Require Telegram to use the instance-scoped OutputBatch byte route."""

    async def dispatch(self, request: Request, call_next):
        if (
            request.method == "GET"
            and _LEGACY_CONTENT_PATH.fullmatch(request.url.path)
            and request.query_params.get("client_type", "").strip().lower()
            == "telegram"
        ):
            return JSONResponse(
                status_code=409,
                content={
                    "detail": (
                        "Telegram delivery content requires an exact "
                        "instance-owned OutputBatch claim"
                    )
                },
            )
        return await call_next(request)
