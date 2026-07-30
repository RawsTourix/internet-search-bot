import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("WEBHOOK_DOMAIN", "https://example.test")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("TELEGRAM_API_KEY", "telegram-test-key")
os.environ.setdefault("GATEWAY_URL", "http://gateway.test")

from src.servers.telegram import batch_commands  # noqa: E402


class TelegramRunStatusCompletionTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _update():
        return SimpleNamespace(
            effective_chat=SimpleNamespace(id=12345),
            effective_message=SimpleNamespace(message_id=200),
        )

    async def test_delivered_receipt_replaces_result_ready_with_cycle_done(self):
        gateway = SimpleNamespace(
            take_completed_output_state=AsyncMock(return_value="delivered"),
        )
        status = SimpleNamespace(message_id=300)
        finish = AsyncMock()

        with patch.object(batch_commands.server, "artifact_gateway", gateway), \
                patch.object(
                    batch_commands.server,
                    "finish_status_or_send_reply",
                    new=finish,
                ):
            await batch_commands._finalize_delivered_run_status(
                update=self._update(),
                status_message=status,
                metadata={
                    "output_batch": {"output_batch_id": "obat-test"},
                },
                locale="ru",
            )

        gateway.take_completed_output_state.assert_awaited_once_with("obat-test")
        finish.assert_awaited_once_with(
            update=self._update(),
            status_message=status,
            text="✅ Задача завершена.",
            delivery_mode="edit_status",
        )

    async def test_non_delivered_receipt_does_not_claim_success(self):
        gateway = SimpleNamespace(
            take_completed_output_state=AsyncMock(
                return_value="partially_delivered"
            ),
        )
        finish = AsyncMock()

        with patch.object(batch_commands.server, "artifact_gateway", gateway), \
                patch.object(
                    batch_commands.server,
                    "finish_status_or_send_reply",
                    new=finish,
                ):
            await batch_commands._finalize_delivered_run_status(
                update=self._update(),
                status_message=SimpleNamespace(message_id=300),
                metadata={
                    "output_batch": {"output_batch_id": "obat-test"},
                },
                locale="ru",
            )

        finish.assert_not_awaited()

    async def test_missing_output_batch_is_a_noop(self):
        gateway = SimpleNamespace(
            take_completed_output_state=AsyncMock(),
        )
        finish = AsyncMock()

        with patch.object(batch_commands.server, "artifact_gateway", gateway), \
                patch.object(
                    batch_commands.server,
                    "finish_status_or_send_reply",
                    new=finish,
                ):
            await batch_commands._finalize_delivered_run_status(
                update=self._update(),
                status_message=SimpleNamespace(message_id=300),
                metadata={},
                locale="ru",
            )

        gateway.take_completed_output_state.assert_not_awaited()
        finish.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
