import unittest

from telegram.error import BadRequest, TimedOut

from src.artifacts.models import new_artifact_delivery_id, new_artifact_id
from src.interaction.ids import (
    new_output_delivery_group_id,
    new_output_part_id,
)
from src.interaction.output_models import (
    ArtifactOutputPart,
    OutputDeliveryGroup,
    OutputPartReceiptState,
    TransportOperationKind,
)
from src.servers.telegram.output_plan_executor import TelegramExecutionContext
from src.servers.telegram.scoped_output_executor import (
    InstanceScopedTelegramOutputPlanExecutor,
)
from tests.telegram_fakes import FakeTelegramBot, FakeTelegramGateway


class _FailFirstOpenGateway(FakeTelegramGateway):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def open_delivery_file(self, delivery_id: str, *, session_id: str):
        if not self.failed:
            self.failed = True
            self.opened.append(delivery_id)
            raise RuntimeError("transient delivery preflight failure")
        return await super().open_delivery_file(
            delivery_id,
            session_id=session_id,
        )


class TelegramDocumentGroupFallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.executor = InstanceScopedTelegramOutputPlanExecutor()
        self.parts = [self._document(0), self._document(1)]
        self.group = OutputDeliveryGroup(
            group_id=new_output_delivery_group_id(),
            index=0,
            operation_kind=TransportOperationKind.DOCUMENT_GROUP,
            part_ids=tuple(part.part_id for part in self.parts),
            required=True,
        )

    async def test_two_confirmed_group_bad_requests_fall_back_to_documents(self):
        bot = FakeTelegramBot()
        bot.queue(
            "send_media_group",
            BadRequest("stream representation rejected"),
            BadRequest("eager representation rejected"),
        )
        gateway = FakeTelegramGateway()

        receipts = await self.executor._execute_group(
            group=self.group,
            parts=self.parts,
            context=self._context(bot, gateway),
            reply_to_message_id=77,
            limits={},
        )

        self.assertEqual(
            [name for name, _ in bot.calls],
            [
                "send_media_group",
                "send_media_group",
                "send_document",
                "send_document",
            ],
        )
        self.assertTrue(
            all(
                receipt.state == OutputPartReceiptState.DELIVERED
                for receipt in receipts
            )
        )
        self.assertEqual(bot.calls[2][1]["reply_to_message_id"], 77)
        self.assertNotIn("reply_to_message_id", bot.calls[3][1])

    async def test_preflight_failure_falls_back_before_any_group_send(self):
        bot = FakeTelegramBot()
        gateway = _FailFirstOpenGateway()

        receipts = await self.executor._execute_group(
            group=self.group,
            parts=self.parts,
            context=self._context(bot, gateway),
            reply_to_message_id=77,
            limits={},
        )

        self.assertEqual(
            [name for name, _ in bot.calls],
            ["send_document", "send_document"],
        )
        self.assertTrue(
            all(
                receipt.state == OutputPartReceiptState.DELIVERED
                for receipt in receipts
            )
        )

    async def test_ambiguous_group_timeout_is_never_retried(self):
        bot = FakeTelegramBot()
        bot.queue("send_media_group", TimedOut())
        gateway = FakeTelegramGateway()

        receipts = await self.executor._execute_group(
            group=self.group,
            parts=self.parts,
            context=self._context(bot, gateway),
            reply_to_message_id=77,
            limits={},
        )

        self.assertEqual(
            [name for name, _ in bot.calls],
            ["send_media_group"],
        )
        self.assertTrue(
            all(
                receipt.state == OutputPartReceiptState.UNKNOWN
                for receipt in receipts
            )
        )

    @staticmethod
    def _context(bot, gateway):
        return TelegramExecutionContext(
            bot=bot,
            gateway=gateway,
            session_id="session-1",
            chat_id=100,
        )

    @staticmethod
    def _document(index: int) -> ArtifactOutputPart:
        return ArtifactOutputPart(
            part_id=new_output_part_id(),
            index=index,
            artifact_id=new_artifact_id(),
            delivery_id=new_artifact_delivery_id(),
            filename=f"document-{index}.txt",
            mime_type="text/plain",
            size_bytes=1,
        )


if __name__ == "__main__":
    unittest.main()
