"""Telegram bridge for execution-scoped progress presentation metadata."""

from __future__ import annotations

from typing import Any

from .artifact_bridge import TelegramArtifactBridgeError
from .collection_bridge import ExplicitCollectionTelegramGatewayClient


class RunScopedProgressTelegramGatewayClient(
    ExplicitCollectionTelegramGatewayClient
):
    """Bind progress to the run status created for this exact invocation.

    InputBatch response routes describe durable reply provenance.  They are not
    the authority for a later `/send` processing message.  The run request
    therefore carries a non-persisted metadata overlay whose status handle is
    valid only for this execution.
    """

    async def run_committed(
        self,
        input_batch_id: str,
        *,
        session_id: str,
        progress_locale: str,
        progress_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        async with self._client() as client:
            response = await client.post(
                f"{self.gateway_url}/input-batches/{input_batch_id}/run",
                json={
                    "session_id": session_id,
                    "progress_locale": progress_locale,
                    "progress_metadata": dict(progress_metadata or {}),
                },
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise TelegramArtifactBridgeError(
                "Gateway run response is invalid"
            )
        return payload
