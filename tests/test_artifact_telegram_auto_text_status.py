import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("WEBHOOK_DOMAIN", "https://example.test")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("TELEGRAM_API_KEY", "telegram-test-key")
os.environ.setdefault("GATEWAY_URL", "http://gateway.test")

from src.servers.telegram import app as telegram_app  # noqa: E402


class TelegramAutoTextStatusTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _update():
        return SimpleNamespace(
            effective_user=SimpleNamespace(language_code="ru"),
            effective_chat=SimpleNamespace(id=12345),
            effective_message=SimpleNamespace(message_id=200),
            update_id=100,
        )

    async def test_committed_text_uses_message_received_status(self):
        status = SimpleNamespace(message_id=300)
        submission = {
            "status": "committed",
            "duplicate": False,
            "input_batch_id": "ibat-test",
            "presentation_event": {
                "message_key": "input_batch.committed",
                "locale": "ru",
                "params": {
                    "file_count": 0,
                    "text_part_count": 1,
                },
            },
        }

        with patch.object(
            telegram_app.server,
            "send_initial_status_message",
            new=AsyncMock(return_value=status),
        ) as send_status, patch.object(
            telegram_app,
            "_base_bind_input_presentation_status",
            new=AsyncMock(),
        ) as bind_status, patch.object(
            telegram_app,
            "_remember_presentation_handle",
            new=AsyncMock(),
        ), patch.object(
            telegram_app,
            "_remember_auto_run_presentation",
            new=AsyncMock(),
        ):
            result = await telegram_app._apply_input_ack_policy(
                update=self._update(),
                submission=submission,
                session_id="telegram:conversation:12345",
            )

        send_status.assert_awaited_once_with(
            self._update(),
            "Сообщение принято. Обрабатываю…",
        )
        bind_status.assert_awaited_once_with(
            submission=submission,
            status_message=status,
            session_id="telegram:conversation:12345",
        )
        self.assertIs(result, status)


if __name__ == "__main__":
    unittest.main()
