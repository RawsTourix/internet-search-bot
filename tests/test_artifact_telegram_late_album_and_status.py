import os
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("WEBHOOK_DOMAIN", "https://example.test")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("TELEGRAM_API_KEY", "telegram-test-key")
os.environ.setdefault("GATEWAY_URL", "http://gateway.test")

from src.servers.telegram import app as telegram_app


class TelegramLateAlbumAndStatusTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _update(*, message_id: int = 10, media_group_id: str = "album-1"):
        return SimpleNamespace(
            update_id=message_id + 1000,
            effective_user=SimpleNamespace(
                id=200,
                full_name="Tester",
                language_code="ru",
            ),
            effective_chat=SimpleNamespace(id=100),
            effective_message=SimpleNamespace(
                message_id=message_id,
                media_group_id=media_group_id,
                message_thread_id=None,
            ),
        )

    async def test_closed_album_update_is_suppressed_before_base_handler(self):
        update = self._update()
        base_handler = AsyncMock()
        is_closed = AsyncMock(return_value=True)

        with (
            patch.object(telegram_app, "_base_attachment_handler", base_handler),
            patch.object(
                telegram_app.server.media_group_runner,
                "is_closed",
                is_closed,
            ),
        ):
            result = await telegram_app._guarded_attachment_handler(
                update,
                SimpleNamespace(),
            )

        self.assertIsNone(result)
        is_closed.assert_awaited_once_with("default:100:-:album-1")
        base_handler.assert_not_awaited()

    async def test_open_album_update_reaches_base_handler(self):
        update = self._update()
        expected = object()
        base_handler = AsyncMock(return_value=expected)
        is_closed = AsyncMock(return_value=False)

        with (
            patch.object(telegram_app, "_base_attachment_handler", base_handler),
            patch.object(
                telegram_app.server.media_group_runner,
                "is_closed",
                is_closed,
            ),
        ):
            result = await telegram_app._guarded_attachment_handler(
                update,
                SimpleNamespace(),
            )

        self.assertIs(result, expected)
        base_handler.assert_awaited_once()

    async def test_pending_collection_callback_edits_authoritative_status(self):
        update = self._update(message_id=50)
        stale_status = SimpleNamespace(message_id=100)
        finish = AsyncMock(return_value=SimpleNamespace(message_id=200))
        stop = AsyncMock()

        with (
            patch.object(
                telegram_app.server,
                "finish_status_or_send_reply",
                finish,
            ),
            patch.object(telegram_app.server, "stop_progress_edits", stop),
        ):
            result = await telegram_app._deliver_agent_result(
                update=update,
                status_message=stale_status,
                success=True,
                message="",
                metadata={
                    "input_collection_pending": True,
                    "presentation_message_id": "200",
                    "file_count": 29,
                    "text_part_count": 7,
                    "progress_locale": "ru",
                },
                session_id="telegram:conversation:100",
            )

        self.assertEqual(result.message_id, 200)
        stop.assert_awaited_once_with(chat_id=100, message_id=100)
        called_status = finish.await_args.kwargs["status_message"]
        self.assertEqual(called_status.message_id, 200)

    async def test_pending_collection_uses_latest_guarded_counts(self):
        update = self._update(message_id=51)
        finish = AsyncMock(return_value=SimpleNamespace(message_id=200))

        class GuardedGateway:
            @asynccontextmanager
            async def explicit_presentation_guard(self, input_batch_id):
                self.input_batch_id = input_batch_id
                yield {
                    "terminal": False,
                    "file_count": 30,
                    "text_part_count": 6,
                    "presentation_message_id": "200",
                }

        gateway = GuardedGateway()
        with (
            patch.object(telegram_app, "artifact_gateway", gateway),
            patch.object(
                telegram_app.server,
                "finish_status_or_send_reply",
                finish,
            ),
        ):
            await telegram_app._deliver_agent_result(
                update=update,
                status_message=None,
                success=True,
                message="",
                metadata={
                    "input_collection_pending": True,
                    "input_batch_id": "ibat_" + "1" * 32,
                    "presentation_message_id": None,
                    "file_count": 29,
                    "text_part_count": 5,
                    "progress_locale": "ru",
                },
                session_id="telegram:conversation:100",
            )

        self.assertEqual(gateway.input_batch_id, "ibat_" + "1" * 32)
        called = finish.await_args.kwargs
        self.assertEqual(called["status_message"].message_id, 200)
        self.assertIn("Файлы: 30", called["text"])
        self.assertIn("Сообщения: 6", called["text"])

    async def test_terminal_collection_suppresses_prepared_stale_update(self):
        update = self._update(message_id=52)
        finish = AsyncMock()

        class TerminalGateway:
            @asynccontextmanager
            async def explicit_presentation_guard(self, input_batch_id):
                yield {
                    "terminal": True,
                    "action": "committed",
                }

        with (
            patch.object(
                telegram_app,
                "artifact_gateway",
                TerminalGateway(),
            ),
            patch.object(
                telegram_app.server,
                "finish_status_or_send_reply",
                finish,
            ),
        ):
            result = await telegram_app._deliver_agent_result(
                update=update,
                status_message=None,
                success=True,
                message="",
                metadata={
                    "input_collection_pending": True,
                    "input_batch_id": "ibat_" + "2" * 32,
                    "file_count": 29,
                    "text_part_count": 5,
                    "progress_locale": "ru",
                },
                session_id="telegram:conversation:100",
            )

        self.assertIsNone(result)
        finish.assert_not_awaited()

    async def test_status_bypasses_busy_session_dispatcher(self):
        update = self._update(message_id=60, media_group_id=None)
        update.effective_message.text = "/status"
        base = AsyncMock(return_value="status-result")
        submit = Mock()
        with (
            patch.object(telegram_app, "_base_process_update", base),
            patch.object(telegram_app.session_dispatcher, "submit", submit),
        ):
            result = await telegram_app._queued_process_update(update)
        self.assertEqual(result, "status-result")
        base.assert_awaited_once_with(update)
        submit.assert_not_called()

    async def test_status_command_does_not_create_processing_message(self):
        update = self._update(message_id=70, media_group_id=None)
        update.effective_message.text = "/status"
        send_status = AsyncMock()
        deliver = AsyncMock()
        with (
            patch.object(
                telegram_app.server,
                "send_initial_status_message",
                send_status,
            ),
            patch.object(
                telegram_app.server,
                "send_to_gateway",
                AsyncMock(return_value=(True, "status", {})),
            ),
            patch.object(
                telegram_app.server,
                "_deliver_agent_result",
                deliver,
            ),
            patch.object(
                telegram_app.server.media_group_activity,
                "snapshot_all",
                AsyncMock(return_value={"groups": 0, "in_flight": 0}),
            ),
        ):
            await telegram_app.server.command_handler(
                update,
                SimpleNamespace(),
            )

        send_status.assert_not_awaited()
        self.assertIsNone(deliver.await_args.kwargs["status_message"])

    async def test_explicit_standalone_file_reuses_collection_status(self):
        update = self._update(message_id=71, media_group_id=None)
        send_status = AsyncMock()
        deliver = AsyncMock()
        gateway = SimpleNamespace(
            is_explicit_collection_active=AsyncMock(return_value=True),
            submit_envelope=AsyncMock(return_value={
                "status": "collecting",
                "input_batch_id": "ibat_" + "3" * 32,
                "duplicate": False,
            }),
            commit_and_run=AsyncMock(return_value={
                "status": "collecting",
                "response": "",
                "metadata": {
                    "input_collection_pending": True,
                    "input_batch_id": "ibat_" + "3" * 32,
                },
            }),
        )
        envelope = SimpleNamespace(attachment_slots=[])
        with (
            patch.object(telegram_app.server, "artifact_gateway", gateway),
            patch.object(
                telegram_app.server,
                "send_initial_status_message",
                send_status,
            ),
            patch.object(
                telegram_app.server,
                "build_telegram_input_envelope",
                Mock(return_value=envelope),
            ),
            patch.object(
                telegram_app.server,
                "bind_input_presentation_status",
                AsyncMock(),
            ),
            patch.object(
                telegram_app.server,
                "_deliver_agent_result",
                deliver,
            ),
        ):
            await telegram_app.server._process_standalone_attachment(
                update,
                semantic_parts=[],
            )

        send_status.assert_not_awaited()
        self.assertIsNone(deliver.await_args.kwargs["status_message"])

    def test_text_grouping_error_is_not_reported_as_unsafe_file(self):
        error = telegram_app.server.TelegramArtifactBridgeError(
            "ambiguous transport grouping"
        )
        text = telegram_app.server._safe_transport_error(
            error,
            locale="ru",
            input_kind="message",
        )
        self.assertNotIn("безопасно обработать файл", text.lower())
        self.assertIn("входящее сообщение", text.lower())


if __name__ == "__main__":
    unittest.main()
