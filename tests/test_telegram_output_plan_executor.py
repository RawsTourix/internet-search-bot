import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.error import TimedOut

from src.artifacts.models import new_artifact_delivery_id, new_artifact_id
from src.ingress.models import ClientResponseRoute
from src.interaction.capabilities import (
    ClientCapabilityDeclaration,
    build_default_capability_registry,
    build_telegram_capability_declaration,
)
from src.interaction.ids import (
    new_output_attempt_id,
    new_output_part_id,
)
from src.interaction.output_models import (
    ArtifactOutputPart,
    ContactOutputPart,
    ImageOutputPart,
    LocationOutputPart,
    OutputBatchKind,
    OutputDeliveryReceiptState,
    OutputPartReceiptState,
    TextOutputPart,
)
from src.interaction.output_store import build_ready_output_batch
from src.interaction.rendering import CapabilityOutputRenderer
from src.localization.service import LocalizationService
from src.interaction.config import LocalizationConfigType
from src.servers.telegram.output_plan_executor import (
    TelegramExecutionContext,
    TelegramOutputPlanExecutor,
)
from src.servers.telegram import telegram_server
from tests.telegram_fakes import FakeTelegramBot, FakeTelegramGateway


class TelegramOutputPlanExecutorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = build_default_capability_registry()
        self.snapshot = self.registry.resolve(
            build_telegram_capability_declaration(),
            client_type="telegram",
            client_instance_id="bot-1",
        )
        self.renderer = CapabilityOutputRenderer(
            LocalizationService.from_directory(
                config=LocalizationConfigType()
            )
        )
        self.executor = TelegramOutputPlanExecutor()

    async def _execute(self, parts, *, snapshot=None, bot=None):
        batch = build_ready_output_batch(
            session_id="session-1",
            cycle_id="cycle-1",
            sequence_number=1,
            kind=OutputBatchKind.FINAL,
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="chat-1",
            ),
            locale="en",
            capability_snapshot=snapshot or self.snapshot,
            parts=tuple(parts),
        )
        plan = self.renderer.plan(batch)
        fake_bot = bot or FakeTelegramBot()
        gateway = FakeTelegramGateway()
        receipt = await self.executor.execute(
            batch=batch,
            plan=plan,
            attempt_id=new_output_attempt_id(),
            context=TelegramExecutionContext(
                bot=fake_bot,
                gateway=gateway,
                session_id="session-1",
                chat_id=1,
                reply_to_message_id=9,
            ),
        )
        return fake_bot, gateway, receipt

    async def test_native_location_contact_and_image_use_exact_methods(self):
        image = self._image(0)
        location = LocationOutputPart(
            part_id=new_output_part_id(),
            index=1,
            latitude=57.6261,
            longitude=39.8845,
        )
        contact = ContactOutputPart(
            part_id=new_output_part_id(),
            index=2,
            phone_number="+100000000",
            first_name="Ada",
        )
        bot, _, receipt = await self._execute(
            [image, location, contact]
        )
        self.assertEqual(
            [name for name, _ in bot.calls],
            ["send_photo", "send_location", "send_contact"],
        )
        self.assertEqual(
            [item.index for item in receipt.part_receipts],
            [0, 1, 2],
        )
        self.assertTrue(
            all(
                item.state == OutputPartReceiptState.DELIVERED
                for item in receipt.part_receipts
            )
        )

    async def test_location_without_capability_uses_localized_text_fallback(self):
        snapshot = self.registry.resolve(
            ClientCapabilityDeclaration(
                capability_contract_version=1,
                features=("output.text",),
            ),
            client_type="telegram",
            client_instance_id="bot-1",
        )
        part = LocationOutputPart(
            part_id=new_output_part_id(),
            index=0,
            latitude=1,
            longitude=2,
            title="Place",
        )
        bot, _, receipt = await self._execute(
            [part],
            snapshot=snapshot,
        )
        self.assertEqual([name for name, _ in bot.calls], ["send_message"])
        self.assertIn("Location:", bot.calls[0][1]["text"])
        self.assertEqual(
            receipt.state,
            OutputDeliveryReceiptState.DELIVERED,
        )

    async def test_mixed_output_follows_output_part_order(self):
        parts = [
            TextOutputPart(
                part_id=new_output_part_id(),
                index=0,
                text="A",
            ),
            self._document(1),
            LocationOutputPart(
                part_id=new_output_part_id(),
                index=2,
                latitude=1,
                longitude=2,
            ),
            self._document(3),
        ]
        bot, _, receipt = await self._execute(parts)
        self.assertEqual(
            [name for name, _ in bot.calls],
            [
                "send_message",
                "send_document",
                "send_location",
                "send_document",
            ],
        )
        self.assertEqual(
            [item.index for item in receipt.part_receipts],
            [0, 1, 2, 3],
        )

    async def test_document_group_chunk_sizes_never_send_single_media_group(self):
        for count, expected in {
            1: ["send_document"],
            2: ["send_media_group"],
            10: ["send_media_group"],
            11: ["send_media_group", "send_document"],
            20: ["send_media_group", "send_media_group"],
            21: [
                "send_media_group",
                "send_media_group",
                "send_document",
            ],
        }.items():
            with self.subTest(count=count):
                parts = [self._document(index) for index in range(count)]
                bot, _, receipt = await self._execute(parts)
                self.assertEqual(
                    [name for name, _ in bot.calls],
                    expected,
                )
                self.assertEqual(
                    len(receipt.part_receipts),
                    count,
                )

    async def test_media_group_timeout_and_receipt_mismatch_are_unknown(self):
        parts = [self._document(0), self._document(1)]
        timed_out = FakeTelegramBot()
        timed_out.queue("send_media_group", TimedOut())
        bot, _, receipt = await self._execute(parts, bot=timed_out)
        self.assertEqual(len(bot.calls), 1)
        self.assertEqual(
            receipt.state,
            OutputDeliveryReceiptState.UNKNOWN,
        )
        self.assertTrue(
            all(
                item.state == OutputPartReceiptState.UNKNOWN
                for item in receipt.part_receipts
            )
        )

        mismatch = FakeTelegramBot()
        mismatch.queue("send_media_group", 1)
        bot, _, receipt = await self._execute(parts, bot=mismatch)
        self.assertEqual(len(bot.calls), 1)
        self.assertEqual(
            receipt.state,
            OutputDeliveryReceiptState.UNKNOWN,
        )

    async def test_text_timeout_is_never_reported_delivered(self):
        bot = FakeTelegramBot()
        bot.queue("send_message", TimedOut())
        _, _, receipt = await self._execute(
            [
                TextOutputPart(
                    part_id=new_output_part_id(),
                    index=0,
                    text="result",
                )
            ],
            bot=bot,
        )
        self.assertEqual(
            receipt.part_receipts[0].state,
            OutputPartReceiptState.UNKNOWN,
        )
        self.assertNotEqual(
            receipt.state,
            OutputDeliveryReceiptState.DELIVERED,
        )

    async def test_receipt_api_failure_never_publishes_false_done(self):
        batch = build_ready_output_batch(
            session_id="session-1",
            cycle_id="cycle-1",
            sequence_number=1,
            kind=OutputBatchKind.FINAL,
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="chat-1",
            ),
            locale="ru",
            capability_snapshot=self.snapshot,
            parts=(
                TextOutputPart(
                    part_id=new_output_part_id(),
                    index=0,
                    text="result",
                ),
            ),
        )
        plan = self.renderer.plan(batch)
        receipt = await self.executor.execute(
            batch=batch,
            plan=plan,
            attempt_id=new_output_attempt_id(),
            context=TelegramExecutionContext(
                bot=FakeTelegramBot(),
                gateway=FakeTelegramGateway(),
                session_id="session-1",
                chat_id=1,
            ),
        )
        gateway = SimpleNamespace(
            claim_output_batch=AsyncMock(return_value={
                "output_batch": batch.model_dump(mode="json"),
                "delivery_plan": plan.model_dump(mode="json"),
                "attempt_id": receipt.attempt_id,
            }),
            complete_output_batch=AsyncMock(
                side_effect=RuntimeError("receipt storage unavailable")
            ),
        )
        executor = SimpleNamespace(
            execute=AsyncMock(return_value=receipt)
        )
        finish = AsyncMock()
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=1),
            effective_message=SimpleNamespace(
                message_id=10,
                message_thread_id=None,
            ),
            effective_user=SimpleNamespace(language_code="ru"),
        )
        with (
            patch.object(telegram_server, "artifact_gateway", gateway),
            patch.object(
                telegram_server,
                "telegram_output_executor",
                executor,
            ),
            patch.object(
                telegram_server,
                "finish_status_or_send_reply",
                finish,
            ),
        ):
            await telegram_server._deliver_agent_result(
                update=update,
                status_message=SimpleNamespace(message_id=20),
                success=True,
                message="",
                metadata={
                    "progress_locale": "ru",
                    "output_batch": {
                        "output_batch_id": batch.output_batch_id,
                    },
                },
                session_id="session-1",
            )

        gateway.complete_output_batch.assert_awaited_once()
        finish.assert_awaited_once()
        final_text = finish.await_args.kwargs["text"]
        self.assertIn("подтверждение доставки сохранить", final_text)
        self.assertNotEqual(final_text, "Готово.")

    @staticmethod
    def _document(index):
        return ArtifactOutputPart(
            part_id=new_output_part_id(),
            index=index,
            artifact_id=new_artifact_id(),
            delivery_id=new_artifact_delivery_id(),
            filename=f"{index}.txt",
            mime_type="text/plain",
            size_bytes=1,
        )

    @staticmethod
    def _image(index):
        return ImageOutputPart(
            part_id=new_output_part_id(),
            index=index,
            artifact_id=new_artifact_id(),
            delivery_id=new_artifact_delivery_id(),
            filename="image.png",
            mime_type="image/png",
            size_bytes=1,
            caption="image",
        )
