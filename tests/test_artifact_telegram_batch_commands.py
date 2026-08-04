import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("WEBHOOK_DOMAIN", "https://example.test")
os.environ.setdefault("WEBHOOK_SECRET", "test-secret")
os.environ.setdefault("TELEGRAM_API_KEY", "telegram-test-key")
os.environ.setdefault("GATEWAY_URL", "http://gateway.test")

from telegram.ext import ApplicationHandlerStop  # noqa: E402

from src.servers.telegram import batch_commands  # noqa: E402


class TelegramBatchCommandTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _update(command: str, *, update_id=100, message_id=200):
        message = SimpleNamespace(
            text=command,
            message_id=message_id,
            message_thread_id=None,
        )
        return SimpleNamespace(
            update_id=update_id,
            effective_message=message,
            effective_chat=SimpleNamespace(id=12345),
            effective_user=SimpleNamespace(id=67890),
        )

    def _server_patches(self, gateway):
        status_message = SimpleNamespace(message_id=300)
        progress_metadata = {
            "progress_callback_url": "http://telegram.test/internal/progress",
            "progress_target": {"chat_id": 12345, "message_id": 300},
        }
        return (
            status_message,
            progress_metadata,
            patch.object(batch_commands.server, "artifact_gateway", gateway),
            patch.object(
                batch_commands.server,
                "detect_progress_locale",
                return_value="ru",
            ),
            patch.object(
                batch_commands.server,
                "_session_for_update",
                return_value="telegram:conversation:12345",
            ),
            patch.object(
                batch_commands.server,
                "send_initial_status_message",
                new=AsyncMock(return_value=status_message),
            ),
            patch.object(
                batch_commands.server,
                "finish_status_or_send_reply",
                new=AsyncMock(),
            ),
            patch.object(
                batch_commands.server,
                "_localized",
                side_effect=lambda key, **kwargs: key,
            ),
            patch.object(
                batch_commands.server,
                "_progress_metadata",
                return_value=progress_metadata,
            ),
            patch.object(
                batch_commands.server,
                "_deliver_agent_result",
                new=AsyncMock(),
            ),
            patch.object(
                batch_commands.server,
                "stop_progress_edits",
                new=AsyncMock(),
            ),
            patch.object(
                batch_commands.server,
                "edit_telegram_message_with_retries",
                new=AsyncMock(),
            ),
        )

    async def test_collect_calls_shared_control_and_renders_started_state(self):
        gateway = SimpleNamespace(
            start_collection=AsyncMock(
                return_value={
                    "status": "started",
                    "file_count": 0,
                    "text_part_count": 0,
                }
            )
        )
        status, _, *patches = self._server_patches(gateway)
        with patches[0], patches[1], patches[2], patches[3] as send_initial, \
                patches[4] as finish, patches[5], patches[6], patches[7], \
                patches[8], patches[9]:
            await batch_commands._handle_input_collection_command(
                self._update("/collect"),
                None,
            )

        gateway.start_collection.assert_awaited_once()
        call = gateway.start_collection.await_args
        self.assertEqual(call.kwargs["session_id"], "telegram:conversation:12345")
        self.assertEqual(call.kwargs["chat_id"], 12345)
        self.assertEqual(call.kwargs["principal_id"], 67890)
        self.assertEqual(call.kwargs["locale"], "ru")
        send_initial.assert_awaited_once()
        finish.assert_awaited_once()
        self.assertEqual(finish.await_args.kwargs["status_message"], status)
        self.assertEqual(
            finish.await_args.kwargs["text"],
            "input_collection.started",
        )

    async def test_send_keeps_collection_snapshot_and_targets_run_status(self):
        gateway = SimpleNamespace(
            send_collection=AsyncMock(
                return_value={
                    "status": "committed",
                    "duplicate": False,
                    "input_batch_id": "ibat_" + "1" * 32,
                    "file_count": 2,
                    "text_part_count": 1,
                    "_telegram_previous_status_message_id": "250",
                }
            ),
            run_committed=AsyncMock(
                return_value={
                    "status": "ok",
                    "response": "agent result",
                    "metadata": {"output_batch_id": "obat-test"},
                }
            ),
        )
        status, progress_metadata, *patches = self._server_patches(gateway)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6], patches[7] as deliver, \
                patches[8] as stop_progress, patches[9] as edit_status:
            await batch_commands._handle_input_collection_command(
                self._update("/send", update_id=101, message_id=201),
                None,
            )

        gateway.send_collection.assert_awaited_once()
        stop_progress.assert_awaited_once_with(chat_id=12345, message_id=250)
        edit_status.assert_awaited_once_with(
            chat_id=12345,
            message_id=250,
            text="input_collection.committed_summary",
            parse_mode=None,
        )
        gateway.run_committed.assert_awaited_once_with(
            "ibat_" + "1" * 32,
            session_id="telegram:conversation:12345",
            progress_locale="ru",
            progress_metadata=progress_metadata,
        )
        deliver.assert_awaited_once()
        self.assertEqual(deliver.await_args.kwargs["status_message"], status)
        self.assertEqual(deliver.await_args.kwargs["message"], "agent result")

    async def test_empty_send_keeps_collection_and_does_not_run(self):
        gateway = SimpleNamespace(
            send_collection=AsyncMock(
                return_value={
                    "status": "empty",
                    "file_count": 0,
                    "text_part_count": 0,
                }
            ),
            run_committed=AsyncMock(),
        )
        _, _, *patches = self._server_patches(gateway)
        with patches[0], patches[1], patches[2], patches[3], \
                patches[4] as finish, patches[5], patches[6], patches[7], \
                patches[8] as stop_progress, patches[9] as edit_status:
            await batch_commands._handle_input_collection_command(
                self._update("/send", update_id=102, message_id=202),
                None,
            )

        gateway.run_committed.assert_not_awaited()
        stop_progress.assert_not_awaited()
        edit_status.assert_not_awaited()
        self.assertEqual(
            finish.await_args.kwargs["text"],
            "input_collection.empty",
        )

    async def test_cancel_terminalizes_snapshot_without_deleting_it(self):
        gateway = SimpleNamespace(
            cancel_collection=AsyncMock(
                return_value={
                    "status": "cancelled",
                    "file_count": 1,
                    "text_part_count": 2,
                    "_telegram_previous_status_message_id": "275",
                }
            )
        )
        _, _, *patches = self._server_patches(gateway)
        with patches[0], patches[1], patches[2], patches[3], \
                patches[4] as finish, patches[5], patches[6], patches[7], \
                patches[8] as stop_progress, patches[9] as edit_status:
            await batch_commands._handle_input_collection_command(
                self._update("/cancel", update_id=103, message_id=203),
                None,
            )

        stop_progress.assert_awaited_once_with(chat_id=12345, message_id=275)
        edit_status.assert_awaited_once_with(
            chat_id=12345,
            message_id=275,
            text="input_collection.cancelled_summary",
            parse_mode=None,
        )
        self.assertEqual(
            finish.await_args.kwargs["text"],
            "input_collection.cancelled",
        )

    async def test_public_handler_stops_legacy_command_group(self):
        gateway = SimpleNamespace(
            cancel_collection=AsyncMock(
                return_value={
                    "status": "not_found",
                    "file_count": 0,
                    "text_part_count": 0,
                }
            )
        )
        _, _, *patches = self._server_patches(gateway)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6], patches[7], patches[8], patches[9]:
            with self.assertRaises(ApplicationHandlerStop):
                await batch_commands.input_collection_command_handler(
                    self._update("/cancel", update_id=104, message_id=204),
                    None,
                )

        gateway.cancel_collection.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
