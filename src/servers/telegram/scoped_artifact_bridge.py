"""Exact Telegram transport authority for ingress and OutputBatch control."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ...interaction.ids import new_output_claim_request_id
from .artifact_bridge import (
    TelegramArtifactBridgeError,
    TelegramArtifactGatewayClient,
    telegram_media_group_key,
)


logger = logging.getLogger("TelegramServer.ScopedBridge")


@dataclass(slots=True)
class _ActiveInputGroup:
    group_key: str
    scope_key: str
    source_group_id: str
    input_batch_id: str | None
    last_activity: float


class InstanceScopedTelegramArtifactGatewayClient(
    TelegramArtifactGatewayClient
):
    """Control-plane client for one exact Telegram bot instance.

    Besides output authority, this bridge owns the process-local Telegram input
    sequencing hint. Telegram sends an album and the following instruction as
    separate updates. Once any member of an album has entered this bridge, one
    adjacent text update in the same chat/thread can be given the exact album
    ``source_group_id`` before either request reaches shared ingress.

    This is transport sequencing, not semantic guessing: no active album means
    ordinary atomic text, while more than one active album is an explicit
    ambiguity error.
    """

    def __init__(
        self,
        *,
        client_instance_id: str,
        input_text_join_window_seconds: float = 10.0,
        **values: Any,
    ) -> None:
        super().__init__(**values)
        self.client_instance_id = client_instance_id.strip()
        if not self.client_instance_id:
            raise ValueError("Telegram client instance ID must not be empty")
        if isinstance(input_text_join_window_seconds, bool):
            raise TypeError("Telegram text join window must be numeric")
        self.input_text_join_window_seconds = float(
            input_text_join_window_seconds
        )
        if self.input_text_join_window_seconds <= 0:
            raise ValueError("Telegram text join window must be positive")
        self._input_group_lock = asyncio.Lock()
        self._input_groups: dict[str, _ActiveInputGroup] = {}
        self._input_batch_groups: dict[str, str] = {}

    async def submit_envelope(
        self,
        envelope,
        *,
        progress_locale: str,
    ) -> dict[str, Any]:
        original_group_key = telegram_media_group_key(envelope)
        group_key = original_group_key

        if (
            group_key is None
            and list(getattr(envelope, "text_parts", None) or [])
            and not list(getattr(envelope, "attachment_slots", None) or [])
        ):
            active = await self._resolve_single_active_group(envelope)
            if active is not None:
                envelope = envelope.model_copy(
                    update={"source_group_id": active.source_group_id}
                )
                group_key = active.group_key
                logger.info(
                    "telegram_text_bound_to_active_media_group "
                    "group_key=%s input_batch_id=%s source_message_id=%s",
                    active.group_key,
                    active.input_batch_id,
                    getattr(envelope, "source_message_id", None),
                )

        if group_key is not None:
            await self._register_input_group(group_key, envelope)

        try:
            payload = await super().submit_envelope(
                envelope,
                progress_locale=progress_locale,
            )
        except BaseException:
            # A failed original album member invalidates this local transport
            # workflow. A failed joined text request does not: the exact album
            # remains active so an idempotent text retry can use the same key.
            if original_group_key is not None:
                await self._close_input_group(group_key)
            raise

        batch_id = str(payload.get("input_batch_id") or "").strip()
        if group_key is not None and batch_id:
            async with self._input_group_lock:
                current = self._input_groups.get(group_key)
                if current is not None:
                    current.input_batch_id = batch_id
                    current.last_activity = time.monotonic()
                    self._input_batch_groups[batch_id] = group_key
        return payload

    async def commit_and_run(
        self,
        input_batch_id: str,
        *,
        session_id: str,
        progress_locale: str,
    ) -> dict[str, Any]:
        await self._close_group_for_batch(input_batch_id)
        return await super().commit_and_run(
            input_batch_id,
            session_id=session_id,
            progress_locale=progress_locale,
        )

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

    async def _register_input_group(self, group_key: str, envelope) -> None:
        scope_key = self._input_scope_key(envelope)
        source_group_id = str(
            getattr(envelope, "source_group_id", "") or ""
        ).strip()
        if not scope_key or not source_group_id:
            return
        now = time.monotonic()
        async with self._input_group_lock:
            self._purge_expired_input_groups(now)
            current = self._input_groups.get(group_key)
            if current is None:
                self._input_groups[group_key] = _ActiveInputGroup(
                    group_key=group_key,
                    scope_key=scope_key,
                    source_group_id=source_group_id,
                    input_batch_id=None,
                    last_activity=now,
                )
            else:
                current.last_activity = now

    async def _resolve_single_active_group(self, envelope) -> _ActiveInputGroup | None:
        scope_key = self._input_scope_key(envelope)
        if not scope_key:
            return None
        now = time.monotonic()
        async with self._input_group_lock:
            self._purge_expired_input_groups(now)
            candidates = [
                item
                for item in self._input_groups.values()
                if item.scope_key == scope_key
            ]
            if len(candidates) > 1:
                raise TelegramArtifactBridgeError(
                    "Text input matches multiple active Telegram media groups"
                )
            return candidates[0] if candidates else None

    async def _close_group_for_batch(self, input_batch_id: str) -> None:
        normalized = input_batch_id.strip()
        if not normalized:
            return
        async with self._input_group_lock:
            group_key = self._input_batch_groups.pop(normalized, None)
            if group_key is not None:
                self._input_groups.pop(group_key, None)

    async def _close_input_group(self, group_key: str | None) -> None:
        if not group_key:
            return
        async with self._input_group_lock:
            current = self._input_groups.pop(group_key, None)
            if current is not None and current.input_batch_id:
                self._input_batch_groups.pop(current.input_batch_id, None)

    def _purge_expired_input_groups(self, now: float) -> None:
        expired = [
            key
            for key, item in self._input_groups.items()
            if now - item.last_activity > self.input_text_join_window_seconds
        ]
        for key in expired:
            item = self._input_groups.pop(key)
            if item.input_batch_id:
                self._input_batch_groups.pop(item.input_batch_id, None)

    def _input_scope_key(self, envelope) -> str | None:
        conversation = getattr(envelope, "conversation", None)
        conversation_id = str(
            getattr(conversation, "conversation_id", "") or ""
        ).strip()
        if not conversation_id:
            return None
        thread_id = getattr(conversation, "thread_id", None)
        thread = str(thread_id) if thread_id is not None else "-"
        return f"{self.client_instance_id}:{conversation_id}:{thread}"

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
