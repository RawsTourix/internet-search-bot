import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telegram.ext import ApplicationHandlerStop

from src.servers.telegram import batch_commands


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
        return (
            status_message,
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
                return_value={},
            ),
            patch.object(
                batch_commands.server,
                "_deliver_agent_result",
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
        status, *patches = self._server_patches(gateway)
        with patches[0], patches[1], patches[2], patches[3] as send_initial, \
                patches[4] as finish, patches[5], patches[6], patches[7]:
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

    async def test_send_commits_then_runs_and_delivers_agent_result(self):
        gateway = SimpleNamespace(
            send_collection=AsyncMock(
                return_value={
                    "status": "committed",
                    "duplicate": False,
                    "input_batch_id": "ibat_" + "1" * 32,
                    "file_count": 2,
                    "text_part_count": 1,
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
        status, *patches = self._server_patches(gateway)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6], patches[7] as deliver:
            await batch_commands._handle_input_collection_command(
                self._update("/send", update_id=101, message_id=201),
                None,
            )

        gateway.send_collection.assert_awaited_once()
        gateway.run_committed.assert_awaited_once_with(
            "ibat_" + "1" * 32,
            session_id="telegram:conversation:12345",
            progress_locale="ru",
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
        _, *patches = self._server_patches(gateway)
        with patches[0], patches[1], patches[2], patches[3], \
                patches[4] as finish, patches[5], patches[6], patches[7]:
            await batch_commands._handle_input_collection_command(
                self._update("/done", update_id=102, message_id=202),
                None,
            )

        gateway.run_committed.assert_not_awaited()
        self.assertEqual(
            finish.await_args.kwargs["text"],
            "input_collection.empty",
        )

    async def test_public_handler_stops_legacy_command_group(self):
        gateway = SimpleNamespace(
            cancel_collection=AsyncMock(
                return_value={
                    "status": "cancelled",
                    "file_count": 1,
                    "text_part_count": 2,
                }
            )
        )
        _, *patches = self._server_patches(gateway)
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patches[5], patches[6], patches[7]:
            with self.assertRaises(ApplicationHandlerStop):
                await batch_commands.input_collection_command_handler(
                    self._update("/cancel", update_id=103, message_id=203),
                    None,
                )

        gateway.cancel_collection.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
