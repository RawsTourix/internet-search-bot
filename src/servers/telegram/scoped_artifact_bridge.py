"""Exact Telegram transport authority for OutputBatch claim and receipt."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from ...interaction.ids import new_output_claim_request_id
from .artifact_bridge import (
    TelegramArtifactBridgeError,
    TelegramArtifactGatewayClient,
)


class InstanceScopedTelegramArtifactGatewayClient(
    TelegramArtifactGatewayClient
):
    """Control-plane client for one exact Telegram bot instance.

    Artifact bytes are intentionally not opened by this shared object. The
    executor creates a separate immutable ``TelegramClaimedOutputGateway`` for
    each claimed OutputBatch, which prevents concurrent claims from sharing
    mutable delivery authority.
    """

    def __init__(self, *, client_instance_id: str, **values: Any) -> None:
        super().__init__(**values)
        self.client_instance_id = client_instance_id.strip()
        if not self.client_instance_id:
            raise ValueError("Telegram client instance ID must not be empty")

    def _output_authority(self, session_id: str) -> dict[str, str]:
        normalized_session = session_id.strip()
        if not normalized_session:
            raise ValueError("Output session ID must not be empty")
        return {
            "session_id": normalized_session,
            "client_type": "telegram",
            "client_instance_id": self.client_instance_id,
        }

    async def claim_output_batch(
        self,
        output_batch_id: str,
        *,
        session_id: str,
    ) -> dict[str, Any]:
        claim_request_id = new_output_claim_request_id()
        return await self._post_json_with_retry(
            f"/internal/output-outbox/{output_batch_id}/claim",
            {
                **self._output_authority(session_id),
                "claim_request_id": claim_request_id,
            },
        )

    async def complete_output_batch(
        self,
        output_batch_id: str,
        *,
        session_id: str,
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._post_json_with_retry(
            f"/internal/output-outbox/{output_batch_id}/receipt",
            {
                **self._output_authority(session_id),
                "receipt": receipt,
            },
        )

    async def open_delivery_file(self, *args: Any, **kwargs: Any):
        raise TelegramArtifactBridgeError(
            "Artifact bytes require an immutable claimed OutputBatch gateway"
        )

    async def _post_json_with_retry(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        attempts: int = 3,
    ) -> dict[str, Any]:
        """Retry one idempotent claim or exact receipt payload unchanged."""

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
