"""Process-local Telegram worker for safe READY OutputBatch recovery."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from ...interaction.output_models import (
    ArtifactContentReceiptState,
    ArtifactOutputPart,
    OutputBatch,
    OutputDeliveryPlan,
    OutputDeliveryReceipt,
    OutputDeliveryReceiptState,
    OutputPartReceipt,
    OutputPartReceiptState,
)
from .output_plan_executor import (
    TelegramExecutionContext,
    TelegramOutputPlanExecutor,
)


logger = logging.getLogger("TelegramServer.ReadyOutbox")


class TelegramReadyOutboxWorker:
    """Poll and deliver only final batches for one exact Telegram instance.

    READY is safe to claim because no transport attempt has started. Claimed,
    unknown and terminal states are never listed or automatically resent.
    The durable Gateway claim is the concurrency authority when several worker
    replicas observe the same batch.
    """

    def __init__(
        self,
        *,
        gateway_url: str,
        api_key: str,
        client_instance_id: str,
        bot: Any,
        gateway: Any,
        executor: TelegramOutputPlanExecutor,
        poll_seconds: float = 15.0,
        minimum_age_seconds: float = 30.0,
        batch_limit: int = 50,
    ) -> None:
        self.gateway_url = gateway_url.rstrip("/")
        self.api_key = api_key
        self.client_instance_id = client_instance_id.strip()
        self.bot = bot
        self.gateway = gateway
        self.executor = executor
        self.poll_seconds = float(poll_seconds)
        self.minimum_age_seconds = float(minimum_age_seconds)
        self.batch_limit = int(batch_limit)
        if not self.gateway_url:
            raise ValueError("ready outbox gateway URL must not be empty")
        if not self.api_key:
            raise ValueError("ready outbox API key must not be empty")
        if not self.client_instance_id:
            raise ValueError("ready outbox client instance must not be empty")
        if self.poll_seconds <= 0:
            raise ValueError("ready outbox poll interval must be positive")
        if not 0 <= self.minimum_age_seconds <= 3600:
            raise ValueError("ready outbox minimum age must be between 0 and 3600")
        if not 1 <= self.batch_limit <= 500:
            raise ValueError("ready outbox batch limit must be between 1 and 500")

        self._task: asyncio.Task[None] | None = None
        self.last_success_at: datetime | None = None
        self.last_error_type: str | None = None
        self.completed_count = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._task = asyncio.create_task(
            self._run(),
            name="telegram-ready-output-outbox",
        )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
                self.last_success_at = datetime.now(timezone.utc)
                self.last_error_type = None
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.last_error_type = type(error).__name__
                logger.warning(
                    "telegram_ready_outbox_poll_failed error_type=%s error=%s",
                    type(error).__name__,
                    error,
                )
            await asyncio.sleep(self.poll_seconds)

    async def run_once(self) -> int:
        payload = await self._request_json(
            "GET",
            "/internal/output-outbox/ready",
            params={
                "client_type": "telegram",
                "client_instance_id": self.client_instance_id,
                "limit": self.batch_limit,
                "minimum_age_seconds": self.minimum_age_seconds,
            },
        )
        raw_batches = payload.get("output_batches", [])
        if not isinstance(raw_batches, list):
            raise RuntimeError("Gateway ready outbox response is invalid")

        completed = 0
        for raw_batch in raw_batches:
            try:
                listed = OutputBatch.model_validate(raw_batch)
                if await self._deliver_one(listed):
                    completed += 1
            except asyncio.CancelledError:
                raise
            except httpx.HTTPStatusError as error:
                if error.response.status_code == 409:
                    logger.info(
                        "telegram_ready_outbox_claim_lost output_batch_id=%s",
                        self._batch_id(raw_batch),
                    )
                    continue
                logger.warning(
                    "telegram_ready_outbox_batch_http_failed output_batch_id=%s "
                    "status_code=%s",
                    self._batch_id(raw_batch),
                    error.response.status_code,
                )
            except Exception as error:
                logger.exception(
                    "telegram_ready_outbox_batch_failed output_batch_id=%s "
                    "error_type=%s",
                    self._batch_id(raw_batch),
                    type(error).__name__,
                )
        self.completed_count += completed
        return completed

    async def _deliver_one(self, listed: OutputBatch) -> bool:
        self._validate_listed_authority(listed)
        authority = {
            "session_id": listed.session_id,
            "client_type": "telegram",
            "client_instance_id": self.client_instance_id,
        }
        claim = await self._request_json(
            "POST",
            f"/internal/output-outbox/{listed.output_batch_id}/claim",
            json=authority,
        )
        batch = OutputBatch.model_validate(claim.get("output_batch"))
        plan = OutputDeliveryPlan.model_validate(claim.get("delivery_plan"))
        attempt_id = str(claim.get("attempt_id") or "")
        self._validate_claim(listed, batch, plan, attempt_id)

        try:
            context = self._execution_context(batch)
        except (TypeError, ValueError) as error:
            receipt = self._preflight_failure_receipt(
                batch,
                attempt_id=attempt_id,
                error_category=(
                    "telegram_ready_outbox_invalid_response_route:"
                    f"{type(error).__name__}"
                ),
            )
        else:
            receipt = await self.executor.execute(
                batch=batch,
                plan=plan,
                attempt_id=attempt_id,
                context=context,
            )

        await self._persist_receipt_with_retry(
            batch=batch,
            authority=authority,
            receipt=receipt,
        )
        logger.info(
            "telegram_ready_outbox_batch_completed output_batch_id=%s state=%s",
            batch.output_batch_id,
            receipt.state.value,
        )
        return True

    def _execution_context(self, batch: OutputBatch) -> TelegramExecutionContext:
        route = batch.response_route
        if route.route_type.strip().lower() != "telegram":
            raise ValueError("non-Telegram response route")
        chat_id = self._required_int(route.conversation_id, "conversation_id")
        thread_id = self._optional_int(route.thread_id, "thread_id")
        anchor_id = (
            batch.response_anchor.client_message_id
            if batch.response_anchor is not None
            else None
        )
        reply_id = self._optional_int(
            anchor_id or route.reply_to_message_id,
            "reply_to_message_id",
        )
        return TelegramExecutionContext(
            bot=self.bot,
            gateway=self.gateway,
            session_id=batch.session_id,
            chat_id=chat_id,
            message_thread_id=thread_id,
            reply_to_message_id=reply_id,
            # Recovery does not rely on an ephemeral status-message object. A
            # semantic status part is sent as a new message instead of editing
            # an uncertain presentation handle.
            status_message_id=None,
        )

    async def _persist_receipt_with_retry(
        self,
        *,
        batch: OutputBatch,
        authority: dict[str, str],
        receipt: OutputDeliveryReceipt,
    ) -> None:
        body = {**authority, "receipt": receipt.model_dump(mode="json")}
        last_error: BaseException | None = None
        for attempt in range(3):
            try:
                await self._request_json(
                    "POST",
                    f"/internal/output-outbox/{batch.output_batch_id}/receipt",
                    json=body,
                )
                return
            except httpx.HTTPStatusError as error:
                last_error = error
                if error.response.status_code < 500:
                    raise
            except httpx.RequestError as error:
                last_error = error
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)
        assert last_error is not None
        raise last_error

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        timeout = httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0)
        async with httpx.AsyncClient(
            timeout=timeout,
            headers={"X-API-Key": self.api_key},
        ) as client:
            response = await client.request(
                method,
                f"{self.gateway_url}{path}",
                params=params,
                json=json,
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Gateway outbox response must be an object")
        return payload

    def _validate_listed_authority(self, batch: OutputBatch) -> None:
        snapshot = batch.capability_snapshot
        if snapshot.client_type != "telegram":
            raise ValueError("ready outbox returned another client type")
        if snapshot.client_instance_id != self.client_instance_id:
            raise ValueError("ready outbox returned another client instance")

    @staticmethod
    def _validate_claim(
        listed: OutputBatch,
        claimed: OutputBatch,
        plan: OutputDeliveryPlan,
        attempt_id: str,
    ) -> None:
        if listed.output_batch_id != claimed.output_batch_id:
            raise ValueError("ready outbox claim changed output identity")
        if listed.session_id != claimed.session_id:
            raise ValueError("ready outbox claim changed session authority")
        if plan.output_batch_id != claimed.output_batch_id:
            raise ValueError("ready outbox plan belongs to another batch")
        if not attempt_id:
            raise ValueError("ready outbox claim returned no attempt ID")

    @staticmethod
    def _preflight_failure_receipt(
        batch: OutputBatch,
        *,
        attempt_id: str,
        error_category: str,
    ) -> OutputDeliveryReceipt:
        now = datetime.now(timezone.utc)
        receipts = tuple(
            OutputPartReceipt(
                part_id=part.part_id,
                index=part.index,
                state=OutputPartReceiptState.FAILED,
                required=part.required,
                delivery_id=getattr(part, "delivery_id", None),
                artifact_content_state=(
                    ArtifactContentReceiptState.NOT_DELIVERED
                    if isinstance(part, ArtifactOutputPart)
                    else None
                ),
                error_category=error_category,
            )
            for part in batch.parts
        )
        return OutputDeliveryReceipt(
            output_batch_id=batch.output_batch_id,
            attempt_id=attempt_id,
            state=OutputDeliveryReceiptState.FAILED,
            part_receipts=receipts,
            started_at=now,
            completed_at=now,
        )

    @staticmethod
    def _required_int(value: Any, field_name: str) -> int:
        parsed = TelegramReadyOutboxWorker._optional_int(value, field_name)
        if parsed is None:
            raise ValueError(f"{field_name} must not be empty")
        return parsed

    @staticmethod
    def _optional_int(value: Any, field_name: str) -> int | None:
        if value is None or str(value).strip() == "":
            return None
        if isinstance(value, bool):
            raise TypeError(f"{field_name} must be an integer ID")
        try:
            return int(str(value).strip())
        except (TypeError, ValueError) as error:
            raise ValueError(f"{field_name} must be an integer ID") from error

    @staticmethod
    def _batch_id(raw_batch: Any) -> str:
        if isinstance(raw_batch, dict):
            return str(raw_batch.get("output_batch_id") or "unknown")
        return "unknown"
