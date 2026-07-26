import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.ingress.models import ClientResponseRoute
from src.interaction.capabilities import (
    build_default_capability_registry,
    build_telegram_capability_declaration,
)
from src.interaction.ids import new_output_attempt_id, new_output_part_id
from src.interaction.output_models import (
    OutputBatchKind,
    OutputBatchState,
    OutputDeliveryReceipt,
    OutputDeliveryReceiptState,
    TextOutputPart,
)
from src.interaction.output_outbox import ReadyOutputOutboxService
from src.interaction.output_store import (
    FileSystemOutputBatchStore,
    build_ready_output_batch,
)
from src.interaction.rendering import CapabilityOutputRenderer
from src.servers.telegram.output_plan_executor import TelegramOutputPlanExecutor
from src.servers.telegram.ready_outbox import TelegramReadyOutboxWorker
from tests.telegram_fakes import FakeTelegramBot, FakeTelegramGateway


UTC = timezone.utc


class ReadyOutputOutboxServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = FileSystemOutputBatchStore(self.root)
        registry = build_default_capability_registry()
        declaration = build_telegram_capability_declaration()
        self.bot1 = registry.resolve(
            declaration,
            client_type="telegram",
            client_instance_id="bot-1",
        )
        self.bot2 = registry.resolve(
            declaration,
            client_type="telegram",
            client_instance_id="bot-2",
        )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_projection_is_client_scoped_bounded_and_grace_aware(self):
        now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
        oldest = await self._commit(
            cycle_id="cycle-oldest",
            snapshot=self.bot1,
            ready_at=now - timedelta(minutes=3),
        )
        second = await self._commit(
            cycle_id="cycle-second",
            snapshot=self.bot1,
            ready_at=now - timedelta(minutes=2),
        )
        await self._commit(
            cycle_id="cycle-fresh",
            snapshot=self.bot1,
            ready_at=now - timedelta(seconds=5),
        )
        await self._commit(
            cycle_id="cycle-other-instance",
            snapshot=self.bot2,
            ready_at=now - timedelta(minutes=5),
        )
        delivering = await self._commit(
            cycle_id="cycle-delivering",
            snapshot=self.bot1,
            ready_at=now - timedelta(minutes=4),
        )
        await self.store.claim_delivery(delivering.output_batch_id)

        service = ReadyOutputOutboxService(self.store)
        projected = await service.list_ready(
            client_type="telegram",
            client_instance_id="bot-1",
            limit=1,
            minimum_age_seconds=30,
            now=now,
        )
        self.assertEqual(
            [item.output_batch_id for item in projected],
            [oldest.output_batch_id],
        )

        all_ready = await service.list_ready(
            client_type="telegram",
            client_instance_id="bot-1",
            limit=10,
            minimum_age_seconds=30,
            now=now,
        )
        self.assertEqual(
            [item.output_batch_id for item in all_ready],
            [oldest.output_batch_id, second.output_batch_id],
        )
        self.assertTrue(all(item.state == OutputBatchState.READY for item in all_ready))

    async def _commit(self, *, cycle_id, snapshot, ready_at):
        batch = build_ready_output_batch(
            session_id=f"session-{cycle_id}",
            cycle_id=cycle_id,
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
                    text=cycle_id,
                ),
            ),
            now=ready_at,
        )
        return (await self.store.commit(batch))[0]


class _StubReadyOutboxWorker(TelegramReadyOutboxWorker):
    def __init__(self, *, listed, claimed, plan, bot=None):
        self.listed = listed
        self.claimed = claimed
        self.plan = plan
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        self.receipt_body: dict[str, Any] | None = None
        super().__init__(
            gateway_url="http://gateway.invalid",
            api_key="secret",
            client_instance_id="bot-1",
            bot=bot or FakeTelegramBot(),
            gateway=FakeTelegramGateway(),
            executor=TelegramOutputPlanExecutor(),
            poll_seconds=1,
            minimum_age_seconds=0,
            batch_limit=10,
        )

    async def _request_json(self, method, path, *, params=None, json=None):
        self.requests.append((method, path, {"params": params, "json": json}))
        if method == "GET":
            return {"output_batches": [self.listed.model_dump(mode="json")]}
        if path.endswith("/claim"):
            return {
                "output_batch": self.claimed.model_dump(mode="json"),
                "attempt_id": new_output_attempt_id(),
                "delivery_plan": self.plan.model_dump(mode="json"),
            }
        if path.endswith("/receipt"):
            self.receipt_body = json
            receipt = OutputDeliveryReceipt.model_validate(json["receipt"])
            return {
                **self.claimed.model_dump(mode="json"),
                "state": receipt.state.value,
                "completed_at": receipt.completed_at.isoformat(),
            }
        raise AssertionError(path)


class TelegramReadyOutboxWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        registry = build_default_capability_registry()
        self.snapshot = registry.resolve(
            build_telegram_capability_declaration(),
            client_type="telegram",
            client_instance_id="bot-1",
        )

    async def test_worker_claims_executes_and_persists_exact_receipt(self):
        batch = self._batch(conversation_id="100")
        claimed = batch.model_copy(update={"state": OutputBatchState.DELIVERING})
        plan = CapabilityOutputRenderer().plan(claimed)
        bot = FakeTelegramBot()
        worker = _StubReadyOutboxWorker(
            listed=batch,
            claimed=claimed,
            plan=plan,
            bot=bot,
        )

        self.assertEqual(await worker.run_once(), 1)
        self.assertEqual(len(bot.calls), 1)
        self.assertEqual(bot.calls[0][0], "send_message")
        self.assertIsNotNone(worker.receipt_body)
        receipt = OutputDeliveryReceipt.model_validate(
            worker.receipt_body["receipt"]
        )
        self.assertEqual(receipt.state, OutputDeliveryReceiptState.DELIVERED)
        self.assertEqual(worker.receipt_body["client_type"], "telegram")
        self.assertEqual(worker.receipt_body["client_instance_id"], "bot-1")

    async def test_invalid_route_is_terminally_failed_without_transport(self):
        batch = self._batch(conversation_id="not-an-integer")
        claimed = batch.model_copy(update={"state": OutputBatchState.DELIVERING})
        plan = CapabilityOutputRenderer().plan(claimed)
        bot = FakeTelegramBot()
        worker = _StubReadyOutboxWorker(
            listed=batch,
            claimed=claimed,
            plan=plan,
            bot=bot,
        )

        self.assertEqual(await worker.run_once(), 1)
        self.assertEqual(bot.calls, [])
        receipt = OutputDeliveryReceipt.model_validate(
            worker.receipt_body["receipt"]
        )
        self.assertEqual(receipt.state, OutputDeliveryReceiptState.FAILED)
        self.assertIn(
            "invalid_response_route",
            receipt.part_receipts[0].error_category,
        )

    def _batch(self, *, conversation_id: str):
        return build_ready_output_batch(
            session_id="session-1",
            cycle_id=f"cycle-{conversation_id}",
            sequence_number=1,
            kind=OutputBatchKind.FINAL,
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id=conversation_id,
            ),
            locale="en",
            capability_snapshot=self.snapshot,
            parts=(
                TextOutputPart(
                    part_id=new_output_part_id(),
                    index=0,
                    text="recovered result",
                ),
            ),
        )


if __name__ == "__main__":
    unittest.main()
