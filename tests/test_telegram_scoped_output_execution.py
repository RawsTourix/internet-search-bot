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
from src.servers.telegram.output_plan_executor import TelegramExecutionContext
from src.servers.telegram.scoped_output_executor import (
    InstanceScopedTelegramOutputPlanExecutor,
)
from tests.telegram_fakes import FakeTelegramBot


class _BindingGateway:
    client_instance_id = "bot-1"

    def __init__(self):
        self.bound: list[str] = []
        self.released: list[str] = []

    def bind_output_claim(self, batch):
        self.bound.append(batch.output_batch_id)

    def release_output_claim(self, output_batch_id):
        self.released.append(output_batch_id)


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
        self.gateway = _BindingGateway()
        self.context = TelegramExecutionContext(
            bot=FakeTelegramBot(),
            gateway=self.gateway,
            session_id="session-1",
            chat_id=100,
        )
        self.executor = InstanceScopedTelegramOutputPlanExecutor()

    async def test_claim_projection_wraps_successful_execution(self):
        receipt = await self.executor.execute(
            batch=self.batch,
            plan=CapabilityOutputRenderer().plan(self.batch),
            attempt_id=new_output_attempt_id(),
            context=self.context,
        )
        self.assertEqual(receipt.state.value, "delivered")
        self.assertEqual(self.gateway.bound, [self.batch.output_batch_id])
        self.assertEqual(self.gateway.released, [self.batch.output_batch_id])

    async def test_claim_projection_releases_after_plan_validation_failure(self):
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
        self.assertEqual(self.gateway.bound, [self.batch.output_batch_id])
        self.assertEqual(self.gateway.released, [self.batch.output_batch_id])

    async def test_gateway_instance_must_match_capability_snapshot(self):
        self.gateway.client_instance_id = "bot-2"
        with self.assertRaises(ValueError):
            await self.executor.execute(
                batch=self.batch,
                plan=CapabilityOutputRenderer().plan(self.batch),
                attempt_id=new_output_attempt_id(),
                context=self.context,
            )
        self.assertEqual(self.gateway.bound, [])
        self.assertEqual(self.gateway.released, [])


if __name__ == "__main__":
    unittest.main()
