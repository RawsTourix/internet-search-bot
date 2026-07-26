"""Exact Telegram transport authority for OutputBatch claim and receipt."""

from __future__ import annotations

import asyncio
import hashlib
from tempfile import SpooledTemporaryFile
from typing import Any

import httpx

from ...interaction.ids import new_output_claim_request_id
from .artifact_bridge import (
    TelegramArtifactBridgeError,
    TelegramArtifactGatewayClient,
    _filename_from_disposition,
    _optional_int,
)


class InstanceScopedTelegramArtifactGatewayClient(
    TelegramArtifactGatewayClient
):
    """Route output mutations and bytes through exact instance-owned claims."""

    def __init__(self, *, client_instance_id: str, **values: Any) -> None:
        super().__init__(**values)
        self.client_instance_id = client_instance_id.strip()
        if not self.client_instance_id:
            raise ValueError("Telegram client instance ID must not be empty")
        self._delivery_to_batch: dict[str, str] = {}
        self._batch_to_deliveries: dict[str, set[str]] = {}

    def _output_authority(self, session_id: str) -> dict[str, str]:
        normalized_session = session_id.strip()
        if not normalized_session:
            raise ValueError("Output session ID must not be empty")
        return {
            "session_id": normalized_session,
            "client_type": "telegram",
            "client_instance_id": self.client_instance_id,
        }

    def bind_output_claim(self, batch: Any) -> None:
        """Bind delivery IDs from one immutable claimed OutputBatch in memory.

        The durable Gateway claim remains the authority. This local projection
        only lets the byte-stream method name the exact claimed batch endpoint.
        """

        payload = (
            batch.model_dump(mode="json")
            if hasattr(batch, "model_dump")
            else batch
        )
        if not isinstance(payload, dict):
            raise TelegramArtifactBridgeError("Claimed OutputBatch is invalid")
        output_batch_id = str(payload.get("output_batch_id") or "").strip()
        parts = payload.get("parts")
        if not output_batch_id or not isinstance(parts, list):
            raise TelegramArtifactBridgeError("Claimed OutputBatch is incomplete")

        deliveries: set[str] = set()
        for part in parts:
            if not isinstance(part, dict):
                raise TelegramArtifactBridgeError("Claimed output part is invalid")
            delivery_id = str(part.get("delivery_id") or "").strip()
            if not delivery_id:
                continue
            existing = self._delivery_to_batch.get(delivery_id)
            if existing not in {None, output_batch_id}:
                raise TelegramArtifactBridgeError(
                    "Delivery ID is already bound to another OutputBatch claim"
                )
            deliveries.add(delivery_id)

        self.release_output_claim(output_batch_id)
        self._batch_to_deliveries[output_batch_id] = deliveries
        for delivery_id in deliveries:
            self._delivery_to_batch[delivery_id] = output_batch_id

    def release_output_claim(self, output_batch_id: str) -> None:
        deliveries = self._batch_to_deliveries.pop(output_batch_id, set())
        for delivery_id in deliveries:
            if self._delivery_to_batch.get(delivery_id) == output_batch_id:
                self._delivery_to_batch.pop(delivery_id, None)

    async def claim_output_batch(
        self,
        output_batch_id: str,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        claim_request_id = new_output_claim_request_id()
        payload = await self._post_json_with_retry(
            f"/internal/output-outbox/{output_batch_id}/claim",
            {
                **self._output_authority(session_id),
                "claim_request_id": claim_request_id,
            },
        )
        self.bind_output_claim(payload.get("output_batch"))
        return payload

    async def complete_output_batch(
        self,
        output_batch_id: str,
        *,
        session_id: str,
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            return await self._post_json_with_retry(
                f"/internal/output-outbox/{output_batch_id}/receipt",
                {
                    **self._output_authority(session_id),
                    "receipt": receipt,
                },
            )
        finally:
            # No further byte read is valid after an aggregate receipt attempt.
            # A lost HTTP response is reconciled by exact receipt replay, not by
            # reopening or resending artifact content.
            self.release_output_claim(output_batch_id)

    async def _post_json_with_retry(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        attempts: int = 3,
    ) -> dict[str, Any]:
        last_error: BaseException | None = None
        for attempt in range(attempts):
            try:
                async with self._client(read_timeout=30.0) as client:
                    response = await client.post(
                        f"{self.gateway_url}{path}",
                        json=payload,
                    )
                    response.raise_for_status()
                    result = response.json()
                    if not isinstance(result, dict):
                        raise TelegramArtifactBridgeError(
                            "Gateway output response is invalid"
                        )
                    return result
            except httpx.HTTPStatusError as error:
                last_error = error
                if error.response.status_code < 500:
                    raise
            except httpx.RequestError as error:
                last_error = error
            if attempt + 1 < attempts:
                await asyncio.sleep(2 ** attempt)
        assert last_error is not None
        raise last_error

    async def open_delivery_file(
        self,
        delivery_id: str,
        *,
        session_id: str,
    ) -> tuple[SpooledTemporaryFile, str]:
        output_batch_id = self._delivery_to_batch.get(delivery_id)
        if output_batch_id is None:
            raise TelegramArtifactBridgeError(
                "Delivery content has no active instance-owned OutputBatch claim"
            )

        spool = SpooledTemporaryFile(
            max_size=self.delivery_spool_memory_bytes,
            mode="w+b",
        )
        filename = "artifact.bin"
        try:
            async with self._client(read_timeout=300.0) as client:
                async with client.stream(
                    "GET",
                    f"{self.gateway_url}/internal/output-outbox/"
                    f"{output_batch_id}/deliveries/{delivery_id}/content",
                    params=self._output_authority(session_id),
                ) as response:
                    response.raise_for_status()
                    returned_batch = response.headers.get("x-output-batch-id")
                    if returned_batch != output_batch_id:
                        raise TelegramArtifactBridgeError(
                            "Gateway delivery stream changed OutputBatch identity"
                        )
                    filename = (
                        _filename_from_disposition(
                            response.headers.get("content-disposition", "")
                        )
                        or filename
                    )
                    expected_hash = response.headers.get("x-content-hash")
                    expected_size = _optional_int(
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
                            "Delivery length changed during transport"
                        )
                    actual_hash = "sha256:" + digest.hexdigest()
                    if expected_hash and actual_hash != expected_hash:
                        raise TelegramArtifactBridgeError(
                            "Delivery hash changed during transport"
                        )
            spool.seek(0)
            return spool, filename
        except BaseException:
            spool.close()
            raise
