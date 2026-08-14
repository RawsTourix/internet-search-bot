"""Telegram consumer for durable semantic AgentEmission records."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx
from telegram.error import BadRequest, NetworkError, TimedOut

from ...input_runtime.models import AgentEmission, EmissionState


logger = logging.getLogger("TelegramServer.EmissionOutbox")


class TelegramEmissionOutboxWorker:
    """Deliver durable intermediate messages as new Telegram messages.

    A transport send is never retried blindly. HTTP claim/receipt requests may
    be retried because their identities are durable and idempotent; Telegram
    send itself is attempted exactly once per claimed emission.
    """

    def __init__(
        self,
        *,
        gateway_url: str,
        api_key: str,
        client_instance_id: str,
        bot: Any,
        poll_seconds: float = 15.0,
        batch_limit: int = 50,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.gateway_url = str(gateway_url or "").rstrip("/")
        self.api_key = str(api_key or "")
        self.client_instance_id = str(client_instance_id or "").strip()
        self.bot = bot
        self.poll_seconds = float(poll_seconds)
        self.batch_limit = int(batch_limit)
        self.http_transport = http_transport
        if not self.gateway_url:
            raise ValueError("emission outbox gateway URL must not be empty")
        if not self.api_key:
            raise ValueError("emission outbox API key must not be empty")
        if not self.client_instance_id:
            raise ValueError("emission outbox client instance must not be empty")
        if self.poll_seconds <= 0:
            raise ValueError("emission outbox poll interval must be positive")
        if not 1 <= self.batch_limit <= 200:
            raise ValueError("emission outbox batch limit must be between 1 and 200")
        self._task: asyncio.Task[None] | None = None
        self.completed_count = 0
        self.last_success_at: datetime | None = None
        self.last_error_type: str | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._task = asyncio.create_task(
            self._run(),
            name="telegram-agent-emission-outbox",
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
                    "telegram_emission_outbox_poll_failed error_type=%s error=%s",
                    type(error).__name__,
                    error,
                )
            await asyncio.sleep(self.poll_seconds)

    async def run_once(self) -> int:
        payload = await self._request_json(
            "GET",
            "/internal/emission-outbox/ready",
            params={
                "client_type": "telegram",
                "client_instance_id": self.client_instance_id,
                "limit": self.batch_limit,
            },
        )
        rows = payload.get("emissions", [])
        if not isinstance(rows, list):
            raise RuntimeError("Gateway emission outbox response is invalid")
        completed = 0
        for raw in rows:
            try:
                listed = AgentEmission.model_validate(raw)
                if await self._deliver_one(listed):
                    completed += 1
            except asyncio.CancelledError:
                raise
            except httpx.HTTPStatusError as error:
                if error.response.status_code == 409:
                    logger.info(
                        "telegram_emission_claim_lost emission_id=%s",
                        self._emission_id(raw),
                    )
                    continue
                logger.warning(
                    "telegram_emission_http_failed emission_id=%s status_code=%s",
                    self._emission_id(raw),
                    error.response.status_code,
                )
            except Exception as error:
                logger.exception(
                    "telegram_emission_delivery_failed emission_id=%s error_type=%s",
                    self._emission_id(raw),
                    type(error).__name__,
                )
        self.completed_count += completed
        return completed

    async def _deliver_one(self, listed: AgentEmission) -> bool:
        self._validate_listed(listed)
        claim_token = "emission-claim:" + uuid4().hex
        authority = {
            "session_id": listed.session_id,
            "client_type": "telegram",
            "client_instance_id": self.client_instance_id,
        }
        claim_payload = await self._request_with_retry(
            "POST",
            f"/internal/emission-outbox/{listed.emission_id}/claim",
            json={**authority, "claim_token": claim_token},
        )
        claimed = AgentEmission.model_validate(claim_payload.get("emission"))
        self._validate_claim(listed, claimed, claim_token)

        route = claimed.response_route
        try:
            chat_id = self._required_int(route.get("conversation_id"), "conversation_id")
            thread_id = self._optional_int(route.get("thread_id"), "thread_id")
            reply_id = self._optional_int(
                route.get("reply_to_message_id"),
                "reply_to_message_id",
            )
        except (TypeError, ValueError) as error:
            await self._persist_outcome(
                claimed,
                claim_token=claim_token,
                outcome="failed",
                error_code=f"telegram_route_invalid:{type(error).__name__}",
            )
            return True

        try:
            sent = await self.bot.send_message(
                chat_id=chat_id,
                text=claimed.text,
                message_thread_id=thread_id,
                reply_to_message_id=reply_id,
                parse_mode=None,
            )
        except BadRequest as error:
            await self._persist_outcome(
                claimed,
                claim_token=claim_token,
                outcome="failed",
                error_code=f"telegram_bad_request:{type(error).__name__}",
            )
            return True
        except TypeError as error:
            await self._persist_outcome(
                claimed,
                claim_token=claim_token,
                outcome="failed",
                error_code=f"telegram_preflight:{type(error).__name__}",
            )
            return True
        except (TimedOut, NetworkError) as error:
            await self._persist_outcome(
                claimed,
                claim_token=claim_token,
                outcome="unknown",
                error_code=f"telegram_transport_unknown:{type(error).__name__}",
            )
            return True
        except Exception as error:
            await self._persist_outcome(
                claimed,
                claim_token=claim_token,
                outcome="unknown",
                error_code=f"telegram_transport_unknown:{type(error).__name__}",
            )
            return True

        message_id = getattr(sent, "message_id", None)
        if message_id is None:
            await self._persist_outcome(
                claimed,
                claim_token=claim_token,
                outcome="unknown",
                error_code="telegram_receipt_missing",
            )
            return True
        await self._persist_outcome(
            claimed,
            claim_token=claim_token,
            outcome="delivered",
            external_message_id=str(message_id),
            delivered_at=datetime.now(timezone.utc),
        )
        return True

    async def _persist_outcome(
        self,
        emission: AgentEmission,
        *,
        claim_token: str,
        outcome: str,
        external_message_id: str | None = None,
        delivered_at: datetime | None = None,
        error_code: str | None = None,
    ) -> None:
        route = emission.response_route
        body = {
            "session_id": emission.session_id,
            "cycle_id": emission.cycle_id,
            "generation": emission.generation,
            "client_type": "telegram",
            "client_instance_id": self.client_instance_id,
            "claim_token": claim_token,
            "outcome": outcome,
            "attempt_number": emission.delivery_attempt_count,
            "conversation_id": str(route.get("conversation_id") or ""),
            "thread_id": (
                str(route["thread_id"])
                if route.get("thread_id") is not None
                else None
            ),
            "external_message_id": external_message_id,
            "delivered_at": delivered_at.isoformat() if delivered_at else None,
            "error_code": error_code,
        }
        await self._request_with_retry(
            "POST",
            f"/internal/emission-outbox/{emission.emission_id}/receipt",
            json=body,
        )

    async def _request_with_retry(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any],
    ) -> dict[str, Any]:
        last_error: BaseException | None = None
        for attempt in range(3):
            try:
                return await self._request_json(method, path, json=json)
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
            transport=self.http_transport,
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
            raise RuntimeError("Gateway emission response must be an object")
        return payload

    def _validate_listed(self, emission: AgentEmission) -> None:
        if emission.state != EmissionState.READY:
            raise ValueError("emission outbox returned non-READY record")
        route = emission.response_route
        if str(route.get("client_type") or "").strip().lower() != "telegram":
            raise ValueError("emission outbox returned another client type")
        if str(route.get("client_instance_id") or "").strip() != self.client_instance_id:
            raise ValueError("emission outbox returned another client instance")

    def _validate_claim(
        self,
        listed: AgentEmission,
        claimed: AgentEmission,
        claim_token: str,
    ) -> None:
        if claimed.emission_id != listed.emission_id:
            raise ValueError("emission claim changed stable identity")
        if claimed.session_id != listed.session_id or claimed.cycle_id != listed.cycle_id:
            raise ValueError("emission claim changed runtime authority")
        if claimed.generation != listed.generation:
            raise ValueError("emission claim changed generation")
        if claimed.context_revision_id != listed.context_revision_id:
            raise ValueError("emission claim changed context revision")
        if claimed.state != EmissionState.DELIVERING:
            raise ValueError("emission claim did not enter DELIVERING")
        if claimed.delivery_claim_token != claim_token:
            raise ValueError("emission claim token changed")
        self._validate_listed(
            claimed.model_copy(update={"state": EmissionState.READY})
        )

    @staticmethod
    def _required_int(value: Any, field_name: str) -> int:
        parsed = TelegramEmissionOutboxWorker._optional_int(value, field_name)
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
    def _emission_id(raw: Any) -> str:
        if isinstance(raw, dict):
            return str(raw.get("emission_id") or "unknown")
        return "unknown"


def install_on_ready_worker(ready_worker_type) -> None:
    """Start/stop the semantic worker beside the existing final outbox worker."""

    if getattr(ready_worker_type, "_ir6_emission_worker_installed", False):
        return
    base_start = ready_worker_type.start
    base_stop = ready_worker_type.stop

    async def start_with_emissions(self):
        await base_start(self)
        worker = getattr(self, "_ir6_emission_worker", None)
        if worker is None:
            worker = TelegramEmissionOutboxWorker(
                gateway_url=self.gateway_url,
                api_key=self.api_key,
                client_instance_id=self.client_instance_id,
                bot=self.bot,
                poll_seconds=self.poll_seconds,
                batch_limit=min(self.batch_limit, 200),
                http_transport=self.http_transport,
            )
            self._ir6_emission_worker = worker
        await worker.start()

    async def stop_with_emissions(self):
        worker = getattr(self, "_ir6_emission_worker", None)
        if worker is not None:
            await worker.stop()
        await base_stop(self)

    ready_worker_type.start = start_with_emissions
    ready_worker_type.stop = stop_with_emissions
    ready_worker_type._ir6_emission_worker_installed = True
