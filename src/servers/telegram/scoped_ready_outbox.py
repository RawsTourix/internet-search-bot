"""Instance-scoped READY outbox worker with idempotent claim retries."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from ...interaction.ids import new_output_claim_request_id
from .ready_outbox import TelegramReadyOutboxWorker


class InstanceScopedTelegramReadyOutboxWorker(TelegramReadyOutboxWorker):
    """Retry a lost claim response without creating another delivery attempt."""

    def __init__(self, **values: Any) -> None:
        super().__init__(**values)
        self._claim_request_ids: dict[str, str] = {}

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if method == "POST" and path.endswith("/claim"):
            output_batch_id = self._output_batch_id_from_path(path)
            claim_request_id = self._claim_request_ids.setdefault(
                output_batch_id,
                new_output_claim_request_id(),
            )
            payload = {
                **dict(json or {}),
                "claim_request_id": claim_request_id,
            }
            last_error: BaseException | None = None
            for attempt in range(3):
                try:
                    return await super()._request_json(
                        method,
                        path,
                        params=params,
                        json=payload,
                    )
                except httpx.HTTPStatusError as error:
                    last_error = error
                    if error.response.status_code < 500:
                        self._claim_request_ids.pop(output_batch_id, None)
                        raise
                except httpx.RequestError as error:
                    last_error = error
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
            assert last_error is not None
            raise last_error

        if method == "POST" and path.endswith("/receipt"):
            output_batch_id = self._output_batch_id_from_path(path)
            try:
                result = await super()._request_json(
                    method,
                    path,
                    params=params,
                    json=json,
                )
            except httpx.HTTPStatusError as error:
                if error.response.status_code < 500:
                    self._claim_request_ids.pop(output_batch_id, None)
                raise
            else:
                self._claim_request_ids.pop(output_batch_id, None)
                return result

        return await super()._request_json(
            method,
            path,
            params=params,
            json=json,
        )

    @staticmethod
    def _output_batch_id_from_path(path: str) -> str:
        parts = [item for item in path.split("/") if item]
        try:
            index = parts.index("output-outbox")
            output_batch_id = parts[index + 1]
        except (ValueError, IndexError) as error:
            raise ValueError("invalid OutputBatch outbox path") from error
        if not output_batch_id:
            raise ValueError("OutputBatch ID must not be empty")
        return output_batch_id
