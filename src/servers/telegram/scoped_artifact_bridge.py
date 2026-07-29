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

    This is transport sequencing, not semantic guessing: ordinary text never
    waits, while forwarded text may briefly wait for an earlier forwarded album
    update from the same bot/chat/thread. More than one active album remains an
    explicit ambiguity error.
    """

    def __init__(
        self,
        *,
        client_instance_id: str,
        input_text_join_window_seconds: float = 10.0,
        forwarded_text_join_wait_seconds: float = 1.5,
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
        if isinstance(forwarded_text_join_wait_seconds, bool):
            raise TypeError("Telegram forwarded text join wait must be numeric")
        self.forwarded_text_join_wait_seconds = float(
            forwarded_text_join_wait_seconds
        )
        if self.forwarded_text_join_wait_seconds < 0:
            raise ValueError(
                "Telegram forwarded text join wait must not be negative"
            )
        if (
            self.forwarded_text_join_wait_seconds
            > self.input_text_join_window_seconds
        ):
            raise ValueError(
                "Telegram forwarded text join wait must not exceed the text "
                "join window"
            )
        self._input_group_lock = asyncio.Lock()
        self._input_group_condition = asyncio.Condition(self._input_group_lock)
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
            wait_seconds = (
                self.forwarded_text_join_wait_seconds
                if self._is_forwarded_text(envelope)
                else 0.0
            )
            active = await self._resolve_single_active_group(
                envelope,
                wait_seconds=wait_seconds,
            )
            if active is not None:
                envelope = envelope.model_copy(
                    update={"source_group_id": active.source_group_id}
                )
                group_key = active.group_key
                logger.info(
                    "telegram_text_bound_to_active_media_group "
                    "group_key=%s input_batch_id=%s source_message_id=%s "
                    "forwarded=%s",
                    active.group_key,
                    active.input_batch_id,
                    getattr(envelope, "source_message_id", None),
                    bool(wait_seconds),
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
            async with self._input_group_condition:
                current = self._input_groups.get(group_key)
                if current is not None:
                    current.input_batch_id = batch_id
                    current.last_activity = time.monotonic()
                    self._input_batch_groups[batch_id] = group_key
                    self._input_group_condition.notify_all()
        return payload

    async def commit_and_run(
        self,
        input_batch_id: str,
        *,
        session_id: str,
        progress_locale: str,
    ) -> dict[str, Any]:
        """Commit durably first, then run the agent as a distinct HTTP stage.

        The historical endpoint accepted ``run=true`` and kept one HTTP request
        open for the complete LLM workflow. If Gateway was stopped after commit,
        Telegram logged the whole operation as a commit failure. Splitting the
        stages makes the durable boundary explicit without pretending that the
        current in-process AgentCycle is a recoverable background job.
        """

        await self._close_group_for_batch(input_batch_id)
        logger.info(
            "telegram_input_batch_commit_started input_batch_id=%s "
            "session_id=%s",
            input_batch_id,
            session_id,
        )
        try:
            async with self._client(read_timeout=30.0) as client:
                response = await client.post(
                    f"{self.gateway_url}/input-batches/{input_batch_id}/commit",
                    json={
                        "session_id": session_id,
                        "progress_locale": progress_locale,
                        "run": False,
                    },
                )
                response.raise_for_status()
                commit_payload = response.json()
        except asyncio.CancelledError:
            logger.info(
                "telegram_input_batch_commit_cancelled input_batch_id=%s",
                input_batch_id,
            )
            raise
        except Exception as error:
            logger.exception(
                "telegram_input_batch_commit_failed input_batch_id=%s error=%s",
                input_batch_id,
                type(error).__name__,
            )
            raise

        if not isinstance(commit_payload, dict):
            raise TelegramArtifactBridgeError(
                "Gateway commit response is invalid"
            )

        duplicate = bool(commit_payload.get("duplicate"))
        commit_payload["run_skipped_duplicate"] = duplicate
        logger.info(
            "telegram_input_batch_commit_finished input_batch_id=%s "
            "status=%s duplicate=%s",
            input_batch_id,
            commit_payload.get("status"),
            duplicate,
        )
        if duplicate:
            return commit_payload

        logger.info(
            "telegram_agent_run_started input_batch_id=%s session_id=%s",
            input_batch_id,
            session_id,
        )
        try:
            run_payload = await super().run_committed(
                input_batch_id,
                session_id=session_id,
                progress_locale=progress_locale,
            )
        except asyncio.CancelledError:
            logger.info(
                "telegram_agent_run_cancelled input_batch_id=%s "
                "committed=true",
                input_batch_id,
            )
            raise
        except Exception as error:
            logger.exception(
                "telegram_agent_run_failed input_batch_id=%s committed=true "
                "error_type=%s",
                input_batch_id,
                type(error).__name__,
            )
            raise

        commit_payload["response"] = str(run_payload.get("response") or "")
        commit_payload["metadata"] = dict(run_payload.get("metadata") or {})
        logger.info(
            "telegram_agent_run_finished input_batch_id=%s status=%s",
            input_batch_id,
            run_payload.get("status"),
        )
        return commit_payload

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
        async with self._input_group_condition:
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
            self._input_group_condition.notify_all()

    async def _resolve_single_active_group(
        self,
        envelope,
        *,
        wait_seconds: float = 0.0,
    ) -> _ActiveInputGroup | None:
        scope_key = self._input_scope_key(envelope)
        if not scope_key:
            return None
        deadline = time.monotonic() + max(0.0, wait_seconds)
        waiting_logged = False
        async with self._input_group_condition:
            while True:
                now = time.monotonic()
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
                if candidates:
                    return candidates[0]
                remaining = deadline - now
                if remaining <= 0:
                    if waiting_logged:
                        logger.info(
                            "telegram_forwarded_text_join_wait_expired "
                            "scope_key=%s source_message_id=%s",
                            scope_key,
                            getattr(envelope, "source_message_id", None),
                        )
                    return None
                if not waiting_logged:
                    logger.info(
                        "telegram_forwarded_text_waiting_for_media_group "
                        "scope_key=%s source_message_id=%s wait_seconds=%s",
                        scope_key,
                        getattr(envelope, "source_message_id", None),
                        wait_seconds,
                    )
                    waiting_logged = True
                try:
                    await asyncio.wait_for(
                        self._input_group_condition.wait(),
                        timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    logger.info(
                        "telegram_forwarded_text_join_wait_expired "
                        "scope_key=%s source_message_id=%s",
                        scope_key,
                        getattr(envelope, "source_message_id", None),
                    )
                    return None

    async def _close_group_for_batch(self, input_batch_id: str) -> None:
        normalized = input_batch_id.strip()
        if not normalized:
            return
        async with self._input_group_condition:
            group_key = self._input_batch_groups.pop(normalized, None)
            if group_key is not None:
                self._input_groups.pop(group_key, None)
                self._input_group_condition.notify_all()

    async def _close_input_group(self, group_key: str | None) -> None:
        if not group_key:
            return
        async with self._input_group_condition:
            current = self._input_groups.pop(group_key, None)
            if current is not None and current.input_batch_id:
                self._input_batch_groups.pop(current.input_batch_id, None)
            self._input_group_condition.notify_all()

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

    @staticmethod
    def _is_forwarded_text(envelope) -> bool:
        for part in list(getattr(envelope, "semantic_parts", None) or []):
            raw_type = getattr(part, "type", None)
            part_type = getattr(raw_type, "value", raw_type)
            if str(part_type or "") == "forwarded_message_input":
                return True
        return False

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
