import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("WEBHOOK_DOMAIN", "https://example.test")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("TELEGRAM_API_KEY", "telegram-test-key")
os.environ.setdefault("GATEWAY_URL", "http://gateway.test")

# Importing the canonical composition root installs the strict terminal sender
# into the low-level telegram_server compatibility module.
from src.servers.telegram import app as telegram_app  # noqa: F401, E402
from src.servers.telegram import telegram_server  # noqa: E402


class TelegramTerminalCompatibilityRegressionTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_send_new_uses_public_markdown_reply_seam(self):
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=10))
        status_message = SimpleNamespace(message_id=20)

        with (
            patch.object(
                telegram_server,
                "send_telegram_markdown_reply",
                new_callable=AsyncMock,
            ) as send,
            patch.object(
                telegram_server,
                "edit_telegram_message_with_retries",
                new_callable=AsyncMock,
            ) as edit,
            patch.object(
                telegram_server,
                "stop_progress_edits",
                new_callable=AsyncMock,
            ),
        ):
            await telegram_server.finish_status_or_send_reply(
                update=update,
                status_message=status_message,
                text="Final answer",
                delivery_mode="send_new",
            )

        send.assert_awaited_once_with(update, "Final answer")
        edit.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
