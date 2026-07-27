import unittest

from src.ingress.models import ClientResponseRoute
from src.interaction.capabilities import (
    build_default_capability_registry,
    build_telegram_capability_declaration,
)
from src.interaction.ids import new_output_attempt_id, new_output_part_id
from src.interaction.output_models import OutputBatchKind, TextOutputPart
from src.interaction.output_store import build_ready_output_batch
from src.interaction.rendering import CapabilityOutputRenderer
from src.servers.telegram.output_batch_gateway import TelegramClaimedOutputGateway
from src.servers.telegram.output_plan_executor import (
    TelegramExecutionContext,
    TelegramOutputPlanExecutor,
)
from tests.telegram_fakes import FakeTelegramBot


class _ProductionGateway:
    gateway_url = "http://gateway.test"
    api_key = "secret"
    client_instance_id = "bot-1"
    transport = None
    delivery_spool_memory_bytes = 1024


class _CapturingBaseExecutor(TelegramOutputPlanExecutor):
    def __init__(self):
        super().__init__()
        self.seen_gateway = None

    async def _execute_group(self, **values):
        self.seen_gateway = values["context"].gateway
        return await super()._execute_group(**values)


class TelegramPackageBytePolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_base_executor_scopes_production_gateway_to_output_batch(self):
        snapshot = build_default_capability_registry().resolve(
            build_telegram_capability_declaration(),
            client_type="telegram",
            client_instance_id="bot-1",
        )
        batch = build_ready_output_batch(
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
        executor = _CapturingBaseExecutor()
        context = TelegramExecutionContext(
            bot=FakeTelegramBot(),
            gateway=_ProductionGateway(),
            session_id="session-1",
            chat_id=100,
        )
        receipt = await executor.execute(
            batch=batch,
            plan=CapabilityOutputRenderer().plan(batch),
            attempt_id=new_output_attempt_id(),
            context=context,
        )
        self.assertEqual(receipt.state.value, "delivered")
        self.assertIsInstance(
            executor.seen_gateway,
            TelegramClaimedOutputGateway,
        )
        self.assertEqual(
            executor.seen_gateway.output_batch_id,
            batch.output_batch_id,
        )


if __name__ == "__main__":
    unittest.main()
