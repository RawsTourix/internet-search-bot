"""Prevent transports from bypassing exact OutputBatch delivery authority."""

from __future__ import annotations

import hmac
import os
import re
from urllib.parse import parse_qs

from starlette.datastructures import Headers
from starlette.responses import JSONResponse


_LEGACY_CONTENT_PATH = re.compile(
    r"^/internal/deliveries/[^/]+/content$"
)
_LEGACY_OUTPUT_BATCH_PATH = re.compile(
    r"^/internal/output-batches/[^/]+(?:/(?:claim|receipt|reconcile))?$"
)


class LegacyTelegramDeliveryGuardMiddleware:
    """Keep transport adapters on instance-scoped OutputBatch routes.

    This is deliberately implemented as pure ASGI middleware rather than
    ``BaseHTTPMiddleware``. The latter inserts AnyIO memory streams around every
    request and produces misleading ``WouldBlock``/``CancelledError`` stacks
    when Uvicorn force-cancels an active request during shutdown.

    Legacy aggregate routes have no client-instance contract and therefore are
    reserved for the explicit internal credential. Compatibility byte streaming
    remains available to non-Telegram transports until they migrate, while
    Telegram bytes always require an exact claimed OutputBatch member.
    """

    def __init__(self, app, *, internal_api_key: str | None = None) -> None:
        self.app = app
        self.internal_api_key = (
            str(internal_api_key).strip()
            if internal_api_key is not None
            else os.getenv("INTERNAL_API_KEY", "").strip()
        )

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method") or "").upper()
        path = str(scope.get("path") or "")
        headers = Headers(scope=scope)
        query = parse_qs(
            bytes(scope.get("query_string") or b"").decode(
                "latin-1",
                errors="replace",
            ),
            keep_blank_values=True,
        )
        client_type = str((query.get("client_type") or [""])[0]).strip().lower()

        if (
            method == "GET"
            and _LEGACY_CONTENT_PATH.fullmatch(path)
            and client_type == "telegram"
        ):
            await self._conflict(
                "Telegram delivery content requires an exact "
                "instance-owned OutputBatch claim"
            )(scope, receive, send)
            return

        if _LEGACY_OUTPUT_BATCH_PATH.fullmatch(path):
            supplied = headers.get("X-API-Key", "")
            if (
                not self.internal_api_key
                or not supplied
                or not hmac.compare_digest(supplied, self.internal_api_key)
            ):
                await self._conflict(
                    "Transport output delivery requires the instance-scoped "
                    "OutputBatch outbox API"
                )(scope, receive, send)
                return

        await self.app(scope, receive, send)

    @staticmethod
    def _conflict(detail: str) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content={"detail": detail},
        )
