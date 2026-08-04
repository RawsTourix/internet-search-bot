import os
import unittest
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


if __name__ == "__main__":
    unittest.main()
