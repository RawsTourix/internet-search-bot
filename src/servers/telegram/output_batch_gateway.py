"""OutputBatch-scoped Telegram access to exact artifact delivery bytes."""

from __future__ import annotations

import hashlib
from tempfile import SpooledTemporaryFile
from typing import Any

import httpx
from telegram import InputFile

from .artifact_bridge import TelegramArtifactBridgeError


class TelegramClaimedOutputGateway:
    """Open artifact bytes only through one exact instance-owned OutputBatch.

    The transport executor deliberately receives this bound facade instead of the
    general Telegram gateway client.  Consequently it cannot fall back to the
    legacy per-delivery endpoint or open a delivery that is not a member of the
    currently claimed immutable OutputBatch.
    """

    def __init__(
        self,
        *,
        gateway_url: str,
        api_key: str,
        output_batch_id: str,
        client_instance_id: str,
        transport: httpx.AsyncBaseTransport | None = None,
        delivery_spool_memory_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self.gateway_url = str(gateway_url or "").rstrip("/")
        self.api_key = str(api_key or "")
        self.output_batch_id = str(output_batch_id or "").strip()
        self.client_instance_id = str(client_instance_id or "").strip()
        self.transport = transport
        self.delivery_spool_memory_bytes = int(delivery_spool_memory_bytes)
        if not self.gateway_url:
            raise ValueError("claimed output gateway URL must not be empty")
        if not self.api_key:
            raise ValueError("claimed output gateway API key must not be empty")
        if not self.output_batch_id:
            raise ValueError("claimed output batch ID must not be empty")
        if not self.client_instance_id:
            raise ValueError("claimed output client instance must not be empty")
        if self.delivery_spool_memory_bytes <= 0:
            raise ValueError("claimed output spool limit must be positive")

    @classmethod
    def from_client(
        cls,
        client: Any,
        *,
        output_batch_id: str,
        client_instance_id: str,
    ) -> "TelegramClaimedOutputGateway":
        return cls(
            gateway_url=client.gateway_url,
            api_key=client.api_key,
            output_batch_id=output_batch_id,
            client_instance_id=client_instance_id,
            transport=getattr(client, "transport", None),
            delivery_spool_memory_bytes=getattr(
                client,
                "delivery_spool_memory_bytes",
                8 * 1024 * 1024,
            ),
        )

    async def open_delivery_file(
        self,
        delivery_id: str,
        *,
        session_id: str,
    ) -> tuple[SpooledTemporaryFile, str]:
        """Claim and verify one exact delivery through the scoped outbox route."""

        spool = SpooledTemporaryFile(
            max_size=self.delivery_spool_memory_bytes,
            mode="w+b",
        )
        filename = "artifact.bin"
        try:
            timeout = httpx.Timeout(
                connect=10.0,
                read=300.0,
                write=30.0,
                pool=10.0,
            )
            async with httpx.AsyncClient(
                timeout=timeout,
                transport=self.transport,
                headers={"X-API-Key": self.api_key},
            ) as client:
                async with client.stream(
                    "GET",
                    f"{self.gateway_url}/internal/output-outbox/"
                    f"{self.output_batch_id}/deliveries/{delivery_id}/content",
                    params={
                        "session_id": session_id,
                        "client_type": "telegram",
                        "client_instance_id": self.client_instance_id,
                    },
                ) as response:
                    response.raise_for_status()
                    filename = self._filename_from_disposition(
                        response.headers.get("content-disposition", "")
                    ) or filename
                    expected_hash = response.headers.get("x-content-hash")
                    expected_batch = response.headers.get("x-output-batch-id")
                    if expected_batch and expected_batch != self.output_batch_id:
                        raise TelegramArtifactBridgeError(
                            "Gateway returned bytes for another OutputBatch"
                        )
                    expected_size = self._optional_int(
                        response.headers.get("content-length")
                    )
                    digest = hashlib.sha256()
                    total = 0
                    async for chunk in response.aiter_bytes(64 * 1024):
                        if not chunk:
                            continue
                        spool.write(chunk)
                        digest.update(chunk)
                        total += len(chunk)
                    if expected_size is not None and total != expected_size:
                        raise TelegramArtifactBridgeError(
                            "Delivery length changed during scoped transport"
                        )
                    actual_hash = "sha256:" + digest.hexdigest()
                    if expected_hash and actual_hash != expected_hash:
                        raise TelegramArtifactBridgeError(
                            "Delivery hash changed during scoped transport"
                        )
            spool.seek(0)
            return spool, filename
        except BaseException:
            spool.close()
            raise

    @staticmethod
    def telegram_input_file(
        spool: SpooledTemporaryFile,
        filename: str,
    ) -> InputFile:
        return InputFile(
            spool,
            filename=filename,
            read_file_handle=False,
        )

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        if value is None or str(value).strip() == "":
            return None
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError) as error:
            raise TelegramArtifactBridgeError(
                "Gateway returned an invalid delivery length"
            ) from error
        if parsed < 0:
            raise TelegramArtifactBridgeError(
                "Gateway returned a negative delivery length"
            )
        return parsed

    @staticmethod
    def _filename_from_disposition(value: str) -> str | None:
        from urllib.parse import unquote

        marker = "filename*=UTF-8''"
        if marker in value:
            return unquote(value.split(marker, maxsplit=1)[1].split(";", maxsplit=1)[0])
        if "filename=" in value:
            return value.split("filename=", maxsplit=1)[1].split(";", maxsplit=1)[0].strip('"')
        return None
