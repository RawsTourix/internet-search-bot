import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from telegram.error import BadRequest, TimedOut

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("WEBHOOK_DOMAIN", "https://example.test")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("TELEGRAM_API_KEY", "telegram-test-key")
os.environ.setdefault("GATEWAY_URL", "http://gateway.test")

from src.agent.prompts import ARTIFACT_RULES
from src.artifacts.models import new_artifact_delivery_id, new_artifact_id
from src.core.models import ClientType
from src.ingress.models import (
    ClientAttachmentLocator,
    ClientConversationRef,
    ClientInputEnvelope,
    ClientResponseRoute,
    ClientSenderRef,
    IngressAttachmentSlot,
    IngressTextPart,
)
from src.interaction.capabilities import (
    build_default_capability_registry,
    build_telegram_capability_declaration,
)
from src.interaction.ids import new_output_attempt_id, new_output_part_id
from src.interaction.output_models import (
    ArtifactOutputPart,
    OutputBatchKind,
    OutputBatchState,
    OutputDeliveryReceiptState,
)
from src.interaction.output_startup_recovery import (
    reconcile_unclaimable_legacy_ready,
)
from src.interaction.output_store import (
    FileSystemOutputBatchStore,
    build_ready_output_batch,
)
from src.interaction.rendering import CapabilityOutputRenderer
from src.localization.service import LocalizationService
from src.interaction.config import LocalizationConfigType
from src.servers.telegram import app as telegram_app
from src.servers.telegram.output_plan_executor import TelegramExecutionContext
from src.servers.telegram.scoped_artifact_bridge import (
    InstanceScopedTelegramArtifactGatewayClient,
)
from src.servers.telegram.scoped_output_executor import (
    InstanceScopedTelegramOutputPlanExecutor,
)
from tests.telegram_fakes import FakeTelegramBot, FakeTelegramGateway


class TelegramActiveAlbumTextJoinTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.requests: list[dict] = []
        self.group_batches: dict[str, str] = {}
        self.block_album_one = False
        self.file_request_started = asyncio.Event()
        self.release_file_request = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/ingress/events":
                payload = json.loads(request.content.decode("utf-8"))
                self.requests.append(payload)
                if (
                    self.block_album_one
                    and payload.get("source_group_id") == "album-1"
                    and payload.get("attachment_slots")
                ):
                    self.file_request_started.set()
                    await self.release_file_request.wait()
                group_id = str(payload.get("source_group_id") or "atomic")
                batch_id = self.group_batches.setdefault(
                    group_id,
                    "ibat_" + f"{len(self.group_batches) + 1:032x}",
                )
                return httpx.Response(
                    200,
                    json={
                        "status": "collecting",
                        "input_batch_id": batch_id,
                        "duplicate": False,
                    },
                )
            if request.url.path.endswith("/commit"):
                return httpx.Response(
                    200,
                    json={"status": "committed", "duplicate": False},
                )
            raise AssertionError(f"unexpected request: {request.url}")

        self.client = InstanceScopedTelegramArtifactGatewayClient(
            gateway_url="http://gateway.test",
            api_key="key",
            client_instance_id="default",
            transport=httpx.MockTransport(handler),
            input_text_join_window_seconds=10,
        )

    @staticmethod
    def _file_envelope(group_id: str, message_id: str) -> ClientInputEnvelope:
        return ClientInputEnvelope(
            idempotency_key=f"telegram:file:{message_id}",
            client_type=ClientType.TELEGRAM,
            client_instance_id="default",
            conversation=ClientConversationRef(conversation_id="100"),
            sender=ClientSenderRef(principal_id="200"),
            source_update_id=f"u-{message_id}",
            source_message_id=message_id,
            source_group_id=group_id,
            occurred_at=datetime.now(timezone.utc),
            attachment_slots=[
                IngressAttachmentSlot(
                    slot_id=f"slot-{message_id}",
                    media_kind="document",
                    original_filename=f"{message_id}.txt",
                    declared_mime_type="text/plain",
                    declared_size_bytes=1,
                    transport_locator=ClientAttachmentLocator(
                        provider="telegram",
                        locator=f"file-{message_id}",
                    ),
                )
            ],
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="100",
                reply_to_message_id=message_id,
            ),
        )

    @staticmethod
    def _text_envelope(message_id: str) -> ClientInputEnvelope:
        return ClientInputEnvelope(
            idempotency_key=f"telegram:text:{message_id}",
            client_type=ClientType.TELEGRAM,
            client_instance_id="default",
            conversation=ClientConversationRef(conversation_id="100"),
            sender=ClientSenderRef(principal_id="200"),
            source_update_id=f"u-{message_id}",
            source_message_id=message_id,
            occurred_at=datetime.now(timezone.utc),
            text_parts=[
                IngressTextPart(
                    part_id=f"text-{message_id}",
                    kind="message_text",
                    text="Process the attached package",
                    attachment_slot_ids=[],
                )
            ],
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="100",
                reply_to_message_id=message_id,
            ),
        )

    async def test_text_uses_active_album_while_file_http_is_blocked(self):
        self.block_album_one = True
        file_task = asyncio.create_task(
            self.client.submit_envelope(
                self._file_envelope("album-1", "10"),
                progress_locale="ru",
            )
        )
        await self.file_request_started.wait()
        text_result = await self.client.submit_envelope(
            self._text_envelope("11"),
            progress_locale="ru",
        )
        self.release_file_request.set()
        file_result = await file_task

        self.assertEqual(
            file_result["input_batch_id"],
            text_result["input_batch_id"],
        )
        text_request = next(
            item for item in self.requests if item.get("text_parts")
        )
        self.assertEqual(text_request["source_group_id"], "album-1")

        await self.client.commit_and_run(
            file_result["input_batch_id"],
            session_id="telegram:conversation:100",
            progress_locale="ru",
        )
        await self.client.submit_envelope(
            self._text_envelope("12"),
            progress_locale="ru",
        )
        self.assertIsNone(self.requests[-1].get("source_group_id"))

    async def test_multiple_active_albums_are_explicitly_ambiguous(self):
        await self.client.submit_envelope(
            self._file_envelope("album-a", "20"),
            progress_locale="ru",
        )
        await self.client.submit_envelope(
            self._file_envelope("album-b", "21"),
            progress_locale="ru",
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "multiple active Telegram media groups",
        ):
            await self.client.submit_envelope(
                self._text_envelope("22"),
                progress_locale="ru",
            )


class TelegramNativeDocumentGroupHardeningTests(
    unittest.IsolatedAsyncioTestCase
):
    def setUp(self):
        registry = build_default_capability_registry()
        self.snapshot = registry.resolve(
            build_telegram_capability_declaration(),
            client_type="telegram",
            client_instance_id="bot-1",
        )
        self.renderer = CapabilityOutputRenderer(
            LocalizationService.from_directory(
                config=LocalizationConfigType()
            )
        )
        self.executor = InstanceScopedTelegramOutputPlanExecutor()

    @staticmethod
    def _document(index: int) -> ArtifactOutputPart:
        return ArtifactOutputPart(
            part_id=new_output_part_id(),
            index=index,
            artifact_id=new_artifact_id(),
            delivery_id=new_artifact_delivery_id(),
            filename=f"result-{index}.txt",
            mime_type="text/plain",
            size_bytes=1,
        )

    async def _execute(self, bot: FakeTelegramBot):
        parts = (self._document(0), self._document(1))
        batch = build_ready_output_batch(
            session_id="session-1",
            cycle_id="cycle-1",
            sequence_number=1,
            kind=OutputBatchKind.FINAL,
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="1",
                reply_to_message_id="9",
            ),
            locale="en",
            capability_snapshot=self.snapshot,
            parts=parts,
        )
        receipt = await self.executor.execute(
            batch=batch,
            plan=self.renderer.plan(batch),
            attempt_id=new_output_attempt_id(),
            context=TelegramExecutionContext(
                bot=bot,
                gateway=FakeTelegramGateway(),
                session_id="session-1",
                chat_id=1,
                reply_to_message_id=9,
            ),
        )
        return receipt, bot

    async def test_native_group_uses_direct_handles_with_filenames(self):
        receipt, bot = await self._execute(FakeTelegramBot())
        self.assertEqual(receipt.state, OutputDeliveryReceiptState.DELIVERED)
        name, kwargs = bot.calls[0]
        self.assertEqual(name, "send_media_group")
        self.assertEqual(
            [item.filename for item in kwargs["media"]],
            [
                f"{receipt.part_receipts[0].delivery_id}.bin",
                f"{receipt.part_receipts[1].delivery_id}.bin",
            ],
        )

    async def test_bad_request_is_logged_exactly_then_falls_back(self):
        bot = FakeTelegramBot()
        bot.queue(
            "send_media_group",
            BadRequest("wrong file identifier/HTTP URL specified"),
        )
        with self.assertLogs(
            "TelegramServer.OutputExecutor",
            level="WARNING",
        ) as captured:
            receipt, bot = await self._execute(bot)
        self.assertEqual(receipt.state, OutputDeliveryReceiptState.DELIVERED)
        self.assertEqual(
            [name for name, _ in bot.calls],
            ["send_media_group", "send_document", "send_document"],
        )
        self.assertTrue(
            any(
                "wrong file identifier/HTTP URL specified" in line
                for line in captured.output
            )
        )

    async def test_reply_target_error_retries_group_without_reply(self):
        bot = FakeTelegramBot()
        bot.queue(
            "send_media_group",
            BadRequest("message to be replied not found"),
        )
        receipt, bot = await self._execute(bot)
        self.assertEqual(receipt.state, OutputDeliveryReceiptState.DELIVERED)
        self.assertEqual(
            [name for name, _ in bot.calls],
            ["send_media_group", "send_media_group"],
        )
        self.assertIn("reply_to_message_id", bot.calls[0][1])
        self.assertNotIn("reply_to_message_id", bot.calls[1][1])


class OutputAuthorityStartupRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_ready_is_cancelled_but_real_instance_remains(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = FileSystemOutputBatchStore(Path(temporary))
            registry = build_default_capability_registry()
            declaration = build_telegram_capability_declaration()
            legacy_snapshot = registry.resolve(
                declaration,
                client_type="telegram",
                client_instance_id="legacy-committed-batch:telegram",
            )
            current_snapshot = registry.resolve(
                declaration,
                client_type="telegram",
                client_instance_id="default",
            )
            legacy = build_ready_output_batch(
                session_id="s-legacy",
                cycle_id="c-legacy",
                sequence_number=1,
                kind=OutputBatchKind.FINAL,
                response_route=ClientResponseRoute(
                    route_type="telegram",
                    conversation_id="1",
                ),
                locale="ru",
                capability_snapshot=legacy_snapshot,
                parts=(TelegramNativeDocumentGroupHardeningTests._document(0),),
            )
            current = build_ready_output_batch(
                session_id="s-current",
                cycle_id="c-current",
                sequence_number=1,
                kind=OutputBatchKind.FINAL,
                response_route=ClientResponseRoute(
                    route_type="telegram",
                    conversation_id="1",
                ),
                locale="ru",
                capability_snapshot=current_snapshot,
                parts=(TelegramNativeDocumentGroupHardeningTests._document(0),),
            )
            await store.commit(legacy)
            await store.commit(current)

            report = await reconcile_unclaimable_legacy_ready(store)

            self.assertEqual(
                report.cancelled_legacy_output_batch_ids,
                (legacy.output_batch_id,),
            )
            self.assertEqual(
                (await store.get(legacy.output_batch_id)).state,
                OutputBatchState.CANCELLED,
            )
            self.assertEqual(
                (await store.get(current.output_batch_id)).state,
                OutputBatchState.READY,
            )
            state = store._read(
                store.records / legacy.output_batch_id / "state.json"
            )
            self.assertEqual(
                state["error_code"],
                "unclaimable_legacy_client_instance",
            )
            self.assertEqual(
                report.remaining_ready[0].output_batch_id,
                current.output_batch_id,
            )


class TelegramTerminalDeliveryFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_timeout_updates_known_status_instead_of_disappearing(self):
        update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=100),
        )
        status_message = SimpleNamespace(message_id=200)
        with (
            patch.object(
                telegram_app,
                "_strict_markdown_reply",
                AsyncMock(side_effect=TimedOut()),
            ),
            patch.object(
                telegram_app,
                "_edit_known_status",
                AsyncMock(return_value=status_message),
            ) as edit,
            patch.object(
                telegram_app.server,
                "stop_progress_edits",
                AsyncMock(),
            ),
        ):
            result = await telegram_app._finish_status_or_send_reply(
                update=update,
                status_message=status_message,
                text="Infrastructure interruption",
                delivery_mode="send_new",
            )
        self.assertIs(result, status_message)
        edit.assert_awaited_once_with(
            update,
            status_message,
            "Infrastructure interruption",
        )


class ArtifactPromptFormatRulesTests(unittest.TestCase):
    def test_prompt_requires_explicit_format_and_tool_result_verification(self):
        self.assertIn("явно передавай format_id", ARTIFACT_RULES)
        self.assertIn("filename, format_id, MIME/type", ARTIFACT_RULES)
        self.assertNotIn("определи format_id по расширению", ARTIFACT_RULES)


if __name__ == "__main__":
    unittest.main()
