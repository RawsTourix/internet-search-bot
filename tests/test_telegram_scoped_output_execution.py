import unittest
from datetime import datetime, timezone

from src.ingress.models import ClientResponseRoute
from src.interaction.anchors import (
    ClientResponseAnchor,
    ClientResponseAnchorKind,
)
from src.interaction.capabilities import (
    build_default_capability_registry,
    build_telegram_capability_declaration,
)
from src.interaction.ids import (
    new_output_attempt_id,
    new_output_part_id,
    new_response_anchor_id,
)
from src.interaction.output_models import (
    OutputBatchKind,
    OutputDeliveryPlan,
    OutputPartReceiptState,
    TextOutputPart,
)
from src.interaction.output_store import build_ready_output_batch
from src.interaction.rendering import CapabilityOutputRenderer
from src.servers.telegram.output_batch_gateway import TelegramClaimedOutputGateway
from src.servers.telegram.output_plan_executor import TelegramExecutionContext
from src.servers.telegram.scoped_output_executor import (
    InstanceScopedTelegramOutputPlanExecutor,
)
from tests.telegram_fakes import FakeTelegramBot


class _ProductionGateway:
    gateway_url = "http://gateway.test"
    api_key = "secret"
    client_instance_id = "bot-1"
    transport = None
    delivery_spool_memory_bytes = 1024


class _CapturingExecutor(InstanceScopedTelegramOutputPlanExecutor):
    def __init__(self):
        super().__init__()
        self.seen_context = None

    async def _execute_group(self, **values):
        self.seen_context = values["context"]
        return await super()._execute_group(**values)


class TelegramScopedOutputExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        snapshot = build_default_capability_registry().resolve(
            build_telegram_capability_declaration(),
            client_type="telegram",
            client_instance_id="bot-1",
        )
        now = datetime.now(timezone.utc)
        self.batch = build_ready_output_batch(
            session_id="session-1",
            cycle_id="cycle-1",
            sequence_number=1,
            kind=OutputBatchKind.FINAL,
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="100",
                thread_id="7",
                reply_to_message_id="111",
            ),
            response_anchor=ClientResponseAnchor(
                anchor_id=new_response_anchor_id(),
                client_message_id="321",
                kind=ClientResponseAnchorKind.INSTRUCTION,
                priority=400,
                occurred_at=now,
                selected_at=now,
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
        self.gateway = _ProductionGateway()
        self.bot = FakeTelegramBot()
        self.context = TelegramExecutionContext(
            bot=self.bot,
            gateway=self.gateway,
            session_id="transient-session",
            chat_id=999,
            message_thread_id=99,
            reply_to_message_id=999,
            status_message_id=55,
        )
        self.executor = _CapturingExecutor()

    async def test_execution_uses_immutable_gateway_route_and_anchor(self):
        receipt = await self.executor.execute(
            batch=self.batch,
            plan=CapabilityOutputRenderer().plan(self.batch),
            attempt_id=new_output_attempt_id(),
            context=self.context,
        )
        self.assertEqual(receipt.state.value, "delivered")
        self.assertIsInstance(
            self.executor.seen_context.gateway,
            TelegramClaimedOutputGateway,
        )
        self.assertEqual(
            self.executor.seen_context.gateway.output_batch_id,
            self.batch.output_batch_id,
        )
        self.assertEqual(self.executor.seen_context.session_id, "session-1")
        self.assertEqual(self.executor.seen_context.chat_id, 100)
        self.assertEqual(self.executor.seen_context.message_thread_id, 7)
        self.assertEqual(self.executor.seen_context.reply_to_message_id, 321)
        self.assertEqual(self.executor.seen_context.status_message_id, 55)
        _, kwargs = self.bot.calls[0]
        self.assertEqual(kwargs["chat_id"], 100)
        self.assertEqual(kwargs["message_thread_id"], 7)
        self.assertEqual(kwargs["reply_to_message_id"], 321)
        self.assertIs(self.context.gateway, self.gateway)

    async def test_plan_validation_failure_never_mutates_shared_gateway(self):
        invalid_plan = OutputDeliveryPlan(
            output_batch_id=self.batch.output_batch_id,
            groups=(),
            created_at=self.batch.created_at,
        )
        with self.assertRaises(ValueError):
            await self.executor.execute(
                batch=self.batch,
                plan=invalid_plan,
                attempt_id=new_output_attempt_id(),
                context=self.context,
            )
        self.assertIs(self.context.gateway, self.gateway)
        self.assertEqual(self.bot.calls, [])

    async def test_invalid_durable_route_returns_terminal_preflight_receipt(self):
        invalid = self.batch.model_copy(update={
            "response_route": ClientResponseRoute(
                route_type="telegram",
                conversation_id="not-an-integer",
            )
        })
        receipt = await self.executor.execute(
            batch=invalid,
            plan=CapabilityOutputRenderer().plan(invalid),
            attempt_id=new_output_attempt_id(),
            context=self.context,
        )
        self.assertEqual(receipt.state.value, "failed")
        self.assertEqual(
            receipt.part_receipts[0].state,
            OutputPartReceiptState.FAILED,
        )
        self.assertIn(
            "invalid_response_route",
            receipt.part_receipts[0].error_category,
        )
        self.assertEqual(self.bot.calls, [])

    async def test_gateway_instance_must_match_capability_snapshot(self):
        self.gateway.client_instance_id = "bot-2"
        with self.assertRaises(ValueError):
            await self.executor.execute(
                batch=self.batch,
                plan=CapabilityOutputRenderer().plan(self.batch),
                attempt_id=new_output_attempt_id(),
                context=self.context,
            )
        self.assertIsNone(self.executor.seen_context)
        self.assertEqual(self.bot.calls, [])


if __name__ == "__main__":
    unittest.main()
