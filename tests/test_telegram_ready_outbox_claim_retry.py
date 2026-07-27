import json
import unittest

import httpx

from src.ingress.models import ClientResponseRoute
from src.interaction.capabilities import (
    build_default_capability_registry,
    build_telegram_capability_declaration,
)
from src.interaction.ids import new_output_attempt_id, new_output_part_id
from src.interaction.output_models import (
    OutputBatchKind,
    OutputBatchState,
    TextOutputPart,
)
from src.interaction.output_outbox import ReadyOutputOutboxRef
from src.interaction.output_store import build_ready_output_batch
from src.interaction.rendering import CapabilityOutputRenderer
from src.servers.telegram.output_plan_executor import TelegramOutputPlanExecutor
from src.servers.telegram.scoped_ready_outbox import (
    InstanceScopedTelegramReadyOutboxWorker,
)
from tests.telegram_fakes import FakeTelegramBot, FakeTelegramGateway


class TelegramReadyOutboxClaimRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_lost_claim_response_reuses_same_request_id(self):
        snapshot = build_default_capability_registry().resolve(
            build_telegram_capability_declaration(),
            client_type="telegram",
            client_instance_id="bot-1",
        )
        ready = build_ready_output_batch(
            session_id="session-1",
            cycle_id="cycle-1",
            sequence_number=1,
            kind=OutputBatchKind.FINAL,
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="100",
            ),
            locale="en",
            capability_snapshot=snapshot,
            parts=(
                TextOutputPart(
                    part_id=new_output_part_id(),
                    index=0,
                    text="ready",
                ),
            ),
        )
        claimed = ready.model_copy(update={"state": OutputBatchState.DELIVERING})
        attempt_id = new_output_attempt_id()
        plan = CapabilityOutputRenderer().plan(claimed)
        seen_request_ids: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8"))
            seen_request_ids.append(body["claim_request_id"])
            if len(seen_request_ids) == 1:
                raise httpx.ReadError("claim response was lost", request=request)
            return httpx.Response(
                200,
                json={
                    "output_batch": claimed.model_dump(mode="json"),
                    "attempt_id": attempt_id,
                    "delivery_plan": plan.model_dump(mode="json"),
                },
            )

        worker = InstanceScopedTelegramReadyOutboxWorker(
            gateway_url="http://gateway.test",
            api_key="secret",
            client_instance_id="bot-1",
            bot=FakeTelegramBot(),
            gateway=FakeTelegramGateway(),
            executor=TelegramOutputPlanExecutor(),
            poll_seconds=1,
            minimum_age_seconds=0,
            batch_limit=10,
            http_transport=httpx.MockTransport(handler),
        )
        payload = await worker._claim_with_retry(
            ReadyOutputOutboxRef.from_batch(ready),
            authority={
                "session_id": ready.session_id,
                "client_type": "telegram",
                "client_instance_id": "bot-1",
            },
        )
        self.assertEqual(payload["attempt_id"], attempt_id)
        self.assertEqual(len(seen_request_ids), 2)
        self.assertEqual(seen_request_ids[0], seen_request_ids[1])


if __name__ == "__main__":
    unittest.main()
