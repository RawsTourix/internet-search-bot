"""Exact Telegram transport authority for OutputBatch claim and receipt."""

from __future__ import annotations

from typing import Any

from .artifact_bridge import (
    TelegramArtifactBridgeError,
    TelegramArtifactGatewayClient,
)


class InstanceScopedTelegramArtifactGatewayClient(
    TelegramArtifactGatewayClient
):
    """Route output mutations through exact client-instance outbox APIs."""

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
        async with self._client(read_timeout=30.0) as client:
            response = await client.post(
                f"{self.gateway_url}/internal/output-outbox/"
                f"{output_batch_id}/claim",
                json=self._output_authority(session_id),
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise TelegramArtifactBridgeError(
                    "Gateway OutputBatch claim response is invalid"
                )
            return payload

    async def complete_output_batch(
        self,
        output_batch_id: str,
        *,
        session_id: str,
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        async with self._client(read_timeout=30.0) as client:
            response = await client.post(
                f"{self.gateway_url}/internal/output-outbox/"
                f"{output_batch_id}/receipt",
                json={
                    **self._output_authority(session_id),
                    "receipt": receipt,
                },
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise TelegramArtifactBridgeError(
                    "Gateway OutputBatch receipt response is invalid"
                )
            return payload
