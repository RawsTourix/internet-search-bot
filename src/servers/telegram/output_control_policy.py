"""Package-wide exact OutputBatch control policy for Telegram clients."""

from __future__ import annotations

import asyncio
import os
from typing import Any

import httpx

from ...interaction.ids import new_output_claim_request_id
from .artifact_bridge import (
    TelegramArtifactBridgeError,
    TelegramArtifactGatewayClient,
)


_INSTALLED = False


def install_output_control_policy() -> None:
    """Move the compatibility gateway onto the exact instance-scoped API.

    The canonical control client overrides these methods directly. Installing
    the policy on the base class keeps the low-level webhook entrypoint usable
    without reopening legacy aggregate routes to transport credentials.
    """

    global _INSTALLED
    if _INSTALLED:
        return
    TelegramArtifactGatewayClient.claim_output_batch = _claim_output_batch
    TelegramArtifactGatewayClient.complete_output_batch = _complete_output_batch
    _INSTALLED = True


async def _claim_output_batch(
    self: TelegramArtifactGatewayClient,
    output_batch_id: str,
    *,
    session_id: str,
) -> dict[str, Any]:
    claim_request_id = new_output_claim_request_id()
    return await _post_json_with_retry(
        self,
        f"/internal/output-outbox/{output_batch_id}/claim",
        {
            **_authority(self, session_id),
            "claim_request_id": claim_request_id,
        },
    )


async def _complete_output_batch(
    self: TelegramArtifactGatewayClient,
    output_batch_id: str,
    *,
    session_id: str,
    receipt: dict[str, Any],
) -> dict[str, Any]:
    return await _post_json_with_retry(
        self,
        f"/internal/output-outbox/{output_batch_id}/receipt",
        {
            **_authority(self, session_id),
            "receipt": receipt,
        },
    )


def _authority(
    client: TelegramArtifactGatewayClient,
    session_id: str,
) -> dict[str, str]:
    normalized_session = str(session_id or "").strip()
    if not normalized_session:
        raise ValueError("Output session ID must not be empty")
    client_instance_id = str(
        getattr(client, "client_instance_id", "")
        or os.getenv("TELEGRAM_BOT_INSTANCE_ID", "default")
        or "default"
    ).strip()
    if not client_instance_id:
        client_instance_id = "default"
    return {
        "session_id": normalized_session,
        "client_type": "telegram",
        "client_instance_id": client_instance_id,
    }


async def _post_json_with_retry(
    client: TelegramArtifactGatewayClient,
    path: str,
    payload: dict[str, Any],
    *,
    attempts: int = 3,
) -> dict[str, Any]:
    last_error: BaseException | None = None
    for attempt in range(attempts):
        try:
            async with client._client(read_timeout=30.0) as http_client:
                response = await http_client.post(
                    f"{client.gateway_url}{path}",
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
