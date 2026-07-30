"""Telegram bridge for execution-scoped progress presentation metadata."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any

from .artifact_bridge import TelegramArtifactBridgeError
from .collection_bridge import ExplicitCollectionTelegramGatewayClient


class RunScopedProgressTelegramGatewayClient(
    ExplicitCollectionTelegramGatewayClient
):
    """Bind progress to the status created for one exact run invocation.

    InputBatch response routes describe durable reply provenance. They are not
    presentation authority for a status created after ingress. Explicit `/send`
    passes metadata directly. AUTO text admission creates its status only after
    the committed submission response, so the adapter keeps one bounded,
    one-shot `input_batch_id -> progress metadata` binding until `/run` consumes
    it. This is not a redirect chain and does not make old messages writable.
    """

    def __init__(
        self,
        *,
        maximum_pending_run_presentations: int = 2048,
        **values: Any,
    ) -> None:
        super().__init__(**values)
        if maximum_pending_run_presentations < 1:
            raise ValueError(
                "maximum_pending_run_presentations must be positive"
            )
        self._maximum_pending_run_presentations = int(
            maximum_pending_run_presentations
        )
        self._run_presentation_lock = asyncio.Lock()
        self._pending_run_presentations: OrderedDict[
            str,
            dict[str, Any],
        ] = OrderedDict()
        self._completed_output_states: OrderedDict[str, str] = OrderedDict()

    async def remember_run_presentation(
        self,
        input_batch_id: str,
        *,
        progress_metadata: dict[str, Any],
    ) -> None:
        """Remember one post-ingress AUTO status until exact `/run` consumes it."""

        normalized = input_batch_id.strip()
        metadata = dict(progress_metadata or {})
        target = metadata.get("progress_target")
        if not normalized or not isinstance(target, dict):
            return
        if target.get("chat_id") is None or target.get("message_id") is None:
            return

        async with self._run_presentation_lock:
            self._pending_run_presentations[normalized] = metadata
            self._pending_run_presentations.move_to_end(normalized)
            self._trim_locked(self._pending_run_presentations)

    async def discard_run_presentation(self, input_batch_id: str) -> None:
        normalized = input_batch_id.strip()
        if not normalized:
            return
        async with self._run_presentation_lock:
            self._pending_run_presentations.pop(normalized, None)

    async def _take_run_presentation(
        self,
        input_batch_id: str,
    ) -> dict[str, Any]:
        async with self._run_presentation_lock:
            return dict(
                self._pending_run_presentations.pop(input_batch_id, {})
            )

    async def take_completed_output_state(
        self,
        output_batch_id: str,
    ) -> str | None:
        """Consume the terminal receipt state observed for one OutputBatch."""

        normalized = output_batch_id.strip()
        if not normalized:
            return None
        async with self._run_presentation_lock:
            return self._completed_output_states.pop(normalized, None)

    async def complete_output_batch(
        self,
        output_batch_id: str,
        *,
        session_id: str,
        receipt: dict[str, Any],
    ) -> dict[str, Any]:
        payload = await super().complete_output_batch(
            output_batch_id,
            session_id=session_id,
            receipt=receipt,
        )
        terminal_state = str(
            payload.get("state") or receipt.get("state") or ""
        ).strip()
        normalized = output_batch_id.strip()
        if normalized and terminal_state:
            async with self._run_presentation_lock:
                self._completed_output_states[normalized] = terminal_state
                self._completed_output_states.move_to_end(normalized)
                self._trim_locked(self._completed_output_states)
        return payload

    async def run_committed(
        self,
        input_batch_id: str,
        *,
        session_id: str,
        progress_locale: str,
        progress_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        remembered = await self._take_run_presentation(input_batch_id)
        # `None` means the caller could not pass presentation metadata at the
        # call site and may consume the remembered AUTO binding. An explicit
        # dict, even empty, is authoritative for this invocation and prevents a
        # stale collection status from becoming the run target.
        effective_metadata = (
            remembered
            if progress_metadata is None
            else dict(progress_metadata)
        )
        async with self._client() as client:
            response = await client.post(
                f"{self.gateway_url}/input-batches/{input_batch_id}/run",
                json={
                    "session_id": session_id,
                    "progress_locale": progress_locale,
                    "progress_metadata": effective_metadata,
                },
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise TelegramArtifactBridgeError(
                "Gateway run response is invalid"
            )
        return payload

    def _trim_locked(self, mapping: OrderedDict) -> None:
        while len(mapping) > self._maximum_pending_run_presentations:
            mapping.popitem(last=False)
