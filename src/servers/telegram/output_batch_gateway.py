"""OutputBatch-scoped Telegram access to exact artifact delivery bytes."""

from __future__ import annotations

import hashlib
from pathlib import PurePath
from tempfile import SpooledTemporaryFile
from typing import Any
from urllib.parse import unquote

import httpx
from telegram import InputFile

from .artifact_bridge import TelegramArtifactBridgeError


class TelegramClaimedOutputGateway:
    """Open bytes only through one exact instance-owned OutputBatch claim."""

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
        """Claim and verify one exact delivery through the scoped route."""

        normalized_delivery = str(delivery_id or "").strip()
        normalized_session = str(session_id or "").strip()
        if not normalized_delivery:
            raise ValueError("delivery ID must not be empty")
        if not normalized_session:
            raise ValueError("session ID must not be empty")

        spool = SpooledTemporaryFile(
            max_size=self.delivery_spool_memory_bytes,
            mode="w+b",
        )
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
                    f"{self.output_batch_id}/deliveries/"
                    f"{normalized_delivery}/content",
                    params={
                        "session_id": normalized_session,
                        "client_type": "telegram",
                        "client_instance_id": self.client_instance_id,
                    },
                ) as response:
                    response.raise_for_status()
                    expected_batch = self._required_header(
                        response,
                        "x-output-batch-id",
                    )
                    returned_delivery = self._required_header(
                        response,
                        "x-delivery-id",
                    )
                    expected_hash = self._required_header(
                        response,
                        "x-content-hash",
                    )
                    expected_size = self._required_size(
                        response.headers.get("content-length")
                    )
                    if expected_batch != self.output_batch_id:
                        raise TelegramArtifactBridgeError(
                            "Gateway returned bytes for another OutputBatch"
                        )
                    if returned_delivery != normalized_delivery:
                        raise TelegramArtifactBridgeError(
                            "Gateway returned bytes for another delivery"
                        )
                    if not expected_hash.startswith("sha256:"):
                        raise TelegramArtifactBridgeError(
                            "Gateway returned an unsupported content hash"
                        )
                    filename = self._safe_filename(
                        self._filename_from_disposition(
                            response.headers.get("content-disposition", "")
                        )
                        or "artifact.bin"
                    )

                    digest = hashlib.sha256()
                    total = 0
                    async for chunk in response.aiter_bytes(64 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > expected_size:
                            raise TelegramArtifactBridgeError(
                                "Delivery exceeded its declared length"
                            )
                        spool.write(chunk)
                        digest.update(chunk)
                    if total != expected_size:
                        raise TelegramArtifactBridgeError(
                            "Delivery length changed during scoped transport"
                        )
                    actual_hash = "sha256:" + digest.hexdigest()
                    if actual_hash != expected_hash:
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
    def _required_header(response: httpx.Response, name: str) -> str:
        value = str(response.headers.get(name) or "").strip()
        if not value:
            raise TelegramArtifactBridgeError(
                f"Gateway delivery response lacks {name}"
            )
        return value

    @staticmethod
    def _required_size(value: Any) -> int:
        if value is None or str(value).strip() == "":
            raise TelegramArtifactBridgeError(
                "Gateway delivery response lacks Content-Length"
            )
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
    def _safe_filename(value: str) -> str:
        normalized = str(value or "").replace("\x00", "").strip()
        basename = PurePath(normalized.replace("\\", "/")).name
        if not basename or basename in {".", ".."}:
            return "artifact.bin"
        return basename[:255]

    @staticmethod
    def _filename_from_disposition(value: str) -> str | None:
        marker = "filename*=UTF-8''"
        if marker in value:
            return unquote(
                value.split(marker, maxsplit=1)[1].split(";", maxsplit=1)[0]
            )
        if "filename=" in value:
            return (
                value.split("filename=", maxsplit=1)[1]
                .split(";", maxsplit=1)[0]
                .strip('"')
            )
        return None
