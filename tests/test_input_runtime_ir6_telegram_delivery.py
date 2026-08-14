import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
from telegram.error import BadRequest, TimedOut

from src.input_runtime.models import AgentEmission, EmissionState, new_context_revision_id
from src.servers.telegram.emission_outbox import TelegramEmissionOutboxWorker


NOW = datetime(2026, 8, 8, 14, 0, tzinfo=timezone.utc)


def ready_emission():
    return AgentEmission(
        session_id="telegram:conversation:100:thread:7",
        cycle_id="cycle",
        generation=2,
        context_revision_id=new_context_revision_id(),
        kind="intermediate",
        text="**semantic** <not html>",
        visibility="user",
        importance="normal",
        response_route={
            "client_type": "telegram",
            "client_instance_id": "bot-a",
            "conversation_id": "100",
            "thread_id": "7",
            "reply_to_message_id": "55",
            "capability_snapshot_id": "caps-1",
        },
        state=EmissionState.READY,
        idempotency_key="stable-key",
        created_at=NOW,
    )


class Bot:
    def __init__(self, *, error=None):
        self.error = error
        self.send_calls = []
        self.edit_calls = []

    async def send_message(self, **kwargs):
        self.send_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(message_id=900)

    async def edit_message_text(self, **kwargs):
        self.edit_calls.append(kwargs)
        raise AssertionError("semantic intermediate must never edit progress")


class GatewayTransport:
    def __init__(self, emission, *, lose_claim_once=False, lose_receipt_once=False):
        self.emission = emission
        self.lose_claim_once = lose_claim_once
        self.lose_receipt_once = lose_receipt_once
        self.claim_bodies = []
        self.receipt_bodies = []
        self.list_calls = 0

    async def __call__(self, request):
        path = request.url.path
        if request.method == "GET" and path == "/internal/emission-outbox/ready":
            self.list_calls += 1
            return httpx.Response(200, json={"emissions": [self.emission.model_dump(mode="json")]})
        if request.method == "POST" and path.endswith("/claim"):
            body = json.loads(request.content)
            self.claim_bodies.append(body)
            if self.lose_claim_once and len(self.claim_bodies) == 1:
                raise httpx.ReadTimeout("lost claim response", request=request)
            claimed = self.emission.model_copy(update={
                "state": EmissionState.DELIVERING,
                "delivery_claim_token": body["claim_token"],
                "delivery_claimed_at": NOW,
                "delivery_claim_expires_at": NOW.replace(minute=5),
                "delivery_attempt_count": 1,
            })
            return httpx.Response(200, json={"emission": claimed.model_dump(mode="json")})
        if request.method == "POST" and path.endswith("/receipt"):
            body = json.loads(request.content)
            self.receipt_bodies.append(body)
            if self.lose_receipt_once and len(self.receipt_bodies) == 1:
                raise httpx.ReadTimeout("lost receipt response", request=request)
            terminal = self.emission.model_copy(update={
                "state": (
                    EmissionState.DELIVERED
                    if body["outcome"] == "delivered"
                    else EmissionState.UNKNOWN
                    if body["outcome"] == "unknown"
                    else EmissionState.FAILED
                ),
                "delivered_at": NOW if body["outcome"] == "delivered" else None,
            })
            return httpx.Response(200, json={"emission": terminal.model_dump(mode="json")})
        return httpx.Response(404, json={"detail": "unexpected"})


def worker(bot, transport):
    return TelegramEmissionOutboxWorker(
        gateway_url="http://gateway",
        api_key="key",
        client_instance_id="bot-a",
        bot=bot,
        poll_seconds=15,
        batch_limit=10,
        http_transport=httpx.MockTransport(transport),
    )


def test_semantic_intermediate_is_new_plain_message_not_progress_edit():
    emission = ready_emission()
    bot = Bot()
    gateway = GatewayTransport(emission)
    completed = asyncio.run(worker(bot, gateway).run_once())
    assert completed == 1
    assert len(bot.send_calls) == 1
    assert bot.edit_calls == []
    sent = bot.send_calls[0]
    assert sent["text"] == emission.text
    assert sent["parse_mode"] is None
    assert sent["chat_id"] == 100
    assert sent["message_thread_id"] == 7
    assert sent["reply_to_message_id"] == 55


def test_external_message_reference_is_sent_in_durable_receipt():
    emission = ready_emission()
    gateway = GatewayTransport(emission)
    asyncio.run(worker(Bot(), gateway).run_once())
    assert gateway.receipt_bodies[-1]["external_message_id"] == "900"
    assert gateway.receipt_bodies[-1]["outcome"] == "delivered"
    assert gateway.receipt_bodies[-1]["cycle_id"] == "cycle"
    assert gateway.receipt_bodies[-1]["generation"] == 2


def test_lost_claim_response_reuses_same_claim_token_before_send(monkeypatch):
    emission = ready_emission()
    gateway = GatewayTransport(emission, lose_claim_once=True)
    bot = Bot()

    async def no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    asyncio.run(worker(bot, gateway).run_once())
    assert len(gateway.claim_bodies) == 2
    assert gateway.claim_bodies[0]["claim_token"] == gateway.claim_bodies[1]["claim_token"]
    assert len(bot.send_calls) == 1


def test_lost_receipt_response_retries_receipt_without_second_client_send(monkeypatch):
    emission = ready_emission()
    gateway = GatewayTransport(emission, lose_receipt_once=True)
    bot = Bot()

    async def no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    asyncio.run(worker(bot, gateway).run_once())
    assert len(bot.send_calls) == 1
    assert len(gateway.receipt_bodies) == 2
    assert gateway.receipt_bodies[0] == gateway.receipt_bodies[1]


def test_telegram_bad_request_is_deterministic_failed_receipt():
    emission = ready_emission()
    gateway = GatewayTransport(emission)
    bot = Bot(error=BadRequest("rejected"))
    asyncio.run(worker(bot, gateway).run_once())
    assert gateway.receipt_bodies[-1]["outcome"] == "failed"
    assert gateway.receipt_bodies[-1]["error_code"].startswith("telegram_bad_request")


def test_telegram_timeout_is_unknown_and_not_retried():
    emission = ready_emission()
    gateway = GatewayTransport(emission)
    bot = Bot(error=TimedOut("ambiguous"))
    asyncio.run(worker(bot, gateway).run_once())
    assert len(bot.send_calls) == 1
    assert gateway.receipt_bodies[-1]["outcome"] == "unknown"


def test_worker_rejects_wrong_client_instance_before_claim():
    emission = ready_emission().model_copy(update={
        "response_route": {
            **ready_emission().response_route,
            "client_instance_id": "bot-b",
        }
    })
    gateway = GatewayTransport(emission)
    bot = Bot()
    completed = asyncio.run(worker(bot, gateway).run_once())
    assert completed == 0
    assert gateway.claim_bodies == []
    assert bot.send_calls == []
