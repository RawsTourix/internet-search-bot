import unittest

from src.ingress.models import ClientResponseRoute
from src.interaction.capabilities import (
    build_default_capability_registry,
    build_telegram_capability_declaration,
)
from src.interaction.ids import new_output_attempt_id, new_output_part_id
from src.interaction.output_models import (
    OutputBatchKind,
    OutputDeliveryPlan,
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
        self.seen_gateway = None

    async def _execute_group(self, **values):
        self.seen_gateway = values["context"].gateway
        return await super()._execute_group(**values)


class TelegramScopedOutputExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        snapshot = build_default_capability_registry().resolve(
            build_telegram_capability_declaration(),
            client_type="telegram",
            client_instance_id="bot-1",
        )
        self.batch = build_ready_output_batch(
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
        self.gateway = _ProductionGateway()
        self.bot = FakeTelegramBot()
        self.context = TelegramExecutionContext(
            bot=self.bot,
            gateway=self.gateway,
            session_id="session-1",
            chat_id=100,
        )
        self.executor = _CapturingExecutor()

    async def test_execution_uses_immutable_output_batch_scoped_gateway(self):
        receipt = await self.executor.execute(
            batch=self.batch,
            plan=CapabilityOutputRenderer().plan(self.batch),
            attempt_id=new_output_attempt_id(),
            context=self.context,
        )
        self.assertEqual(receipt.state.value, "delivered")
        self.assertIsInstance(
            self.executor.seen_gateway,
            TelegramClaimedOutputGateway,
        )
        self.assertEqual(
            self.executor.seen_gateway.output_batch_id,
            self.batch.output_batch_id,
        )
        self.assertEqual(
            self.executor.seen_gateway.client_instance_id,
            "bot-1",
        )
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

    async def test_gateway_instance_must_match_capability_snapshot(self):
        self.gateway.client_instance_id = "bot-2"
        with self.assertRaises(ValueError):
            await self.executor.execute(
                batch=self.batch,
                plan=CapabilityOutputRenderer().plan(self.batch),
                attempt_id=new_output_attempt_id(),
                context=self.context,
            )
        self.assertIsNone(self.executor.seen_gateway)
        self.assertEqual(self.bot.calls, [])


if __name__ == "__main__":
    unittest.main()
