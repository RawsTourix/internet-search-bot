"""Telegram client for shared explicit InputBatch collection controls."""

from __future__ import annotations

import asyncio
from typing import Any

from .scoped_artifact_bridge import InstanceScopedTelegramArtifactGatewayClient


class ExplicitCollectionTelegramGatewayClient(
    InstanceScopedTelegramArtifactGatewayClient
):
    """Call shared controls and suppress transport auto-commit for explicit drafts."""

    def __init__(self, **values: Any) -> None:
        super().__init__(**values)
        self._explicit_batch_lock = asyncio.Lock()
        self._explicit_batches: dict[str, dict[str, Any]] = {}

    async def submit_envelope(
        self,
        envelope,
        *,
        progress_locale: str,
    ) -> dict[str, Any]:
        payload = await super().submit_envelope(
            envelope,
            progress_locale=progress_locale,
        )
        params = dict(
            ((payload.get("presentation_event") or {}).get("params") or {})
        )
        batch_id = str(payload.get("input_batch_id") or "").strip()
        if (
            batch_id
            and params.get("assembly_mode") == "explicit"
            and params.get("auto_commit_allowed") is False
        ):
            async with self._explicit_batch_lock:
                self._explicit_batches[batch_id] = {
                    "collection_id": params.get("collection_id"),
                    "file_count": int(params.get("file_count") or 0),
                    "text_part_count": int(params.get("text_part_count") or 0),
                }
        return payload

    async def commit_and_run(
        self,
        input_batch_id: str,
        *,
        session_id: str,
        progress_locale: str,
    ) -> dict[str, Any]:
        async with self._explicit_batch_lock:
            explicit = dict(self._explicit_batches.get(input_batch_id) or {})
        if explicit:
            await self._close_group_for_batch(input_batch_id)
            return {
                "status": "collecting",
                "input_batch_id": input_batch_id,
                "duplicate": False,
                "run_skipped_duplicate": False,
                "response": "",
                "metadata": {
                    "input_collection_pending": True,
                    "collection_id": explicit.get("collection_id"),
                    "file_count": explicit.get("file_count", 0),
                    "text_part_count": explicit.get("text_part_count", 0),
                    "progress_locale": progress_locale,
                },
            }
        return await super().commit_and_run(
            input_batch_id,
            session_id=session_id,
            progress_locale=progress_locale,
        )

    async def start_collection(
        self,
        *,
        session_id: str,
        chat_id: int | str,
        thread_id: int | str | None,
        principal_id: int | str,
        idempotency_key: str,
        locale: str,
        response_route: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._collection_control(
            "start",
            session_id=session_id,
            chat_id=chat_id,
            thread_id=thread_id,
            principal_id=principal_id,
            idempotency_key=idempotency_key,
            extra={
                "locale": locale,
                "response_route": response_route,
            },
        )

    async def inspect_collection(
        self,
        *,
        session_id: str,
        chat_id: int | str,
        thread_id: int | str | None,
        principal_id: int | str,
    ) -> dict[str, Any]:
        return await self._collection_control(
            "inspect",
            session_id=session_id,
            chat_id=chat_id,
            thread_id=thread_id,
            principal_id=principal_id,
        )

    async def send_collection(
        self,
        *,
        session_id: str,
        chat_id: int | str,
        thread_id: int | str | None,
        principal_id: int | str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = await self._collection_control(
            "send",
            session_id=session_id,
            chat_id=chat_id,
            thread_id=thread_id,
            principal_id=principal_id,
            idempotency_key=idempotency_key,
        )
        batch_id = str(payload.get("input_batch_id") or "").strip()
        if payload.get("status") == "committed" and batch_id:
            async with self._explicit_batch_lock:
                self._explicit_batches.pop(batch_id, None)
            await self._close_group_for_batch(batch_id)
        return payload

    async def cancel_collection(
        self,
        *,
        session_id: str,
        chat_id: int | str,
        thread_id: int | str | None,
        principal_id: int | str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        payload = await self._collection_control(
            "cancel",
            session_id=session_id,
            chat_id=chat_id,
            thread_id=thread_id,
            principal_id=principal_id,
            idempotency_key=idempotency_key,
        )
        batch_id = str(payload.get("input_batch_id") or "").strip()
        if batch_id:
            async with self._explicit_batch_lock:
                self._explicit_batches.pop(batch_id, None)
            await self._close_group_for_batch(batch_id)
        return payload

    async def _collection_control(
        self,
        action: str,
        *,
        session_id: str,
        chat_id: int | str,
        thread_id: int | str | None,
        principal_id: int | str,
        idempotency_key: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "session_id": str(session_id),
            "client_type": "telegram",
            "client_instance_id": self.client_instance_id,
            "conversation_id": str(chat_id),
            "thread_id": str(thread_id) if thread_id is not None else None,
            "principal_id": str(principal_id),
        }
        if idempotency_key is not None:
            body["idempotency_key"] = str(idempotency_key)
        body.update(extra or {})
        async with self._client(read_timeout=30.0) as client:
            response = await client.post(
                f"{self.gateway_url}/internal/input-collections/{action}",
                json=body,
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Gateway input collection response is invalid")
        return payload
