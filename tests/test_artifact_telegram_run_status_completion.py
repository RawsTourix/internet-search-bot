import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("WEBHOOK_DOMAIN", "https://example.test")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("TELEGRAM_API_KEY", "telegram-test-key")
os.environ.setdefault("GATEWAY_URL", "http://gateway.test")

from src.servers.telegram import app as telegram_app  # noqa: E402


class TelegramRunStatusCompletionTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _update():
        return SimpleNamespace(
            effective_chat=SimpleNamespace(id=12345),
            effective_message=SimpleNamespace(message_id=200),
        )

    async def _finish(
        self,
        *,
        terminal_state="delivered",
        has_artifacts=False,
        mode="artefacts_only",
    ):
        gateway = SimpleNamespace(
            take_completed_output_state=AsyncMock(
                return_value=terminal_state
            ),
        )
        status = SimpleNamespace(message_id=300)
        core = AsyncMock(return_value=status)
        sender = AsyncMock()
        token = telegram_app._output_completion_context.set({
            "output_batch_id": "obat-test",
            "has_artifacts": has_artifacts,
            "locale": "ru",
        })
        try:
            with patch.object(
                telegram_app.server,
                "artifact_gateway",
                gateway,
            ), patch.object(
                telegram_app,
                "_finish_status_or_send_reply_core",
                new=core,
            ), patch.object(
                telegram_app,
                "_terminal_reply_sender",
                new=Mock(return_value=sender),
            ), patch.object(
                telegram_app,
                "TELEGRAM_FINAL_STATUS_MODE",
                mode,
            ):
                result = await telegram_app._finish_status_or_send_reply(
                    update=self._update(),
                    status_message=status,
                    text="Готово.",
                    delivery_mode="send_new",
                )
        finally:
            telegram_app._output_completion_context.reset(token)
        return gateway, status, core, sender, result

    async def test_delivered_text_finalizes_status_without_extra_done(self):
        gateway, status, core, sender, result = await self._finish()

        gateway.take_completed_output_state.assert_awaited_once_with(
            "obat-test"
        )
        core.assert_awaited_once_with(
            update=self._update(),
            status_message=status,
            text="✅ Задача завершена.",
            delivery_mode="edit_status",
        )
        sender.assert_not_awaited()
        self.assertIs(result, status)

    async def test_default_mode_sends_done_after_artifacts(self):
        _, _, _, sender, _ = await self._finish(has_artifacts=True)

        sender.assert_awaited_once_with(self._update(), "Готово.")

    async def test_always_mode_sends_done_for_text(self):
        _, _, _, sender, _ = await self._finish(mode="always")

        sender.assert_awaited_once_with(self._update(), "Готово.")

    async def test_never_mode_suppresses_done_for_artifacts(self):
        _, _, _, sender, _ = await self._finish(
            has_artifacts=True,
            mode="never",
        )

        sender.assert_not_awaited()

    async def test_partial_delivery_does_not_claim_success(self):
        _, status, core, sender, _ = await self._finish(
            terminal_state="partially_delivered",
            has_artifacts=True,
        )

        core.assert_awaited_once_with(
            update=self._update(),
            status_message=status,
            text="Результат доставлен не полностью.",
            delivery_mode="edit_status",
        )
        sender.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
