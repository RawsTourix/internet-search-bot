"""Strict closed-origin HTTP attachment provider for Gateway ingress."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from .artifact_transport import AttachmentProviderError


@dataclass(slots=True)
class StrictHttpAttachmentStreamProvider:
    """Stream one opaque locator from a fixed internal origin with exact length."""

    base_url: str
    token: str
    provider_name: str
    path_prefix: str = "/internal/files"
    connect_timeout_seconds: float = 10.0
    read_timeout_seconds: float = 120.0

    async def open_stream(
        self,
        locator: str,
        *,
        max_size_bytes: int,
    ) -> AsyncIterator[bytes]:
        normalized = locator.strip()
        if (
            not normalized
            or len(normalized) > 4096
            or any(character in normalized for character in "\r\n\x00")
        ):
            raise AttachmentProviderError("Invalid attachment locator")
        url = (
            self.base_url.rstrip("/")
            + self.path_prefix.rstrip("/")
            + "/"
            + quote(normalized, safe="")
        )
        timeout = httpx.Timeout(
            connect=self.connect_timeout_seconds,
            read=self.read_timeout_seconds,
            write=30.0,
            pool=10.0,
        )

        async def iterator() -> AsyncIterator[bytes]:
            total = 0
            declared_size: int | None = None
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    async with client.stream(
                        "GET",
                        url,
                        headers={"X-File-Provider-Token": self.token},
                    ) as response:
                        if response.status_code == 404:
                            raise AttachmentProviderError(
                                "Attachment provider object was not found"
                            )
                        response.raise_for_status()
                        declared = response.headers.get("content-length")
                        if declared is not None:
                            try:
                                declared_size = int(declared)
                            except ValueError as error:
                                raise AttachmentProviderError(
                                    "Attachment provider returned invalid length"
                                ) from error
                            if declared_size < 0:
                                raise AttachmentProviderError(
                                    "Attachment provider returned invalid length"
                                )
                            if declared_size > max_size_bytes:
                                raise AttachmentProviderError(
                                    "Attachment exceeds the configured size limit"
                                )
                        async for chunk in response.aiter_bytes(64 * 1024):
                            if not chunk:
                                continue
                            total += len(chunk)
                            if total > max_size_bytes:
                                raise AttachmentProviderError(
                                    "Attachment exceeds the configured size limit"
                                )
                            if (
                                declared_size is not None
                                and total > declared_size
                            ):
                                raise AttachmentProviderError(
                                    "Attachment provider length mismatch"
                                )
                            yield chunk
                        if declared_size is not None and total != declared_size:
                            raise AttachmentProviderError(
                                "Attachment provider length mismatch"
                            )
            except AttachmentProviderError:
                raise
            except httpx.HTTPStatusError as error:
                raise AttachmentProviderError(
                    f"Attachment provider HTTP {error.response.status_code}"
                ) from error
            except httpx.HTTPError as error:
                raise AttachmentProviderError(
                    "Attachment provider transport failed"
                ) from error

        return iterator()
