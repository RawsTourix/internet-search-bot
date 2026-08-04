import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.error import BadRequest

from src.servers.telegram.presentation_relocation import (
    apply_relocating_input_ack_policy,
    relocate_precreated_input_presentation,
    replace_unbound_collection_command_status,
)


class TelegramPresentationRelocationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _submission():
        return {
            "ack_policy": "relocate",
            "presentation_event": {
                "message_key": "input_batch.collecting",
                "params": {"file_count": 2, "text_part_count": 1},
                "locale": "ru",
            },
            "presentation_ref": {
                "presentation_id": "iprs_" + "1" * 32,
                "presentation_token": "relocation-token",
                "client_message_id": "100",
                "previous_client_message_id": "100",
                "presentation_generation": 1,
                "relocation_generation": 2,
                "state": "bound",
            },
        }

    @staticmethod
    def _update():
        return SimpleNamespace(
            effective_chat=SimpleNamespace(id=12345),
        )

    @staticmethod
    def _server(*, new_message_id=200):
        gateway = SimpleNamespace(
            relocate_input_presentation=AsyncMock(return_value={"state": "bound"}),
            record_input_presentation_deletion=AsyncMock(
                return_value={"state": "bound"}
            ),
        )
        bot = SimpleNamespace(delete_message=AsyncMock())
        return SimpleNamespace(
            logger=logging.getLogger("test.telegram.relocation"),
            artifact_gateway=gateway,
            application=SimpleNamespace(bot=bot),
            send_initial_status_message=AsyncMock(
                return_value=SimpleNamespace(message_id=new_message_id)
            ),
            stop_progress_edits=AsyncMock(),
            _presentation_text=lambda submission: "collecting",
        )

    async def test_create_bind_then_delete_old_and_record_receipt(self):
        server = self._server()
        base_apply = AsyncMock()

        result = await apply_relocating_input_ack_policy(
            base_apply=base_apply,
            server=server,
            update=self._update(),
            submission=self._submission(),
            session_id="telegram:conversation:12345",
        )

        self.assertEqual(result.message_id, 200)
        base_apply.assert_not_awaited()
        server.artifact_gateway.relocate_input_presentation.assert_awaited_once()
        server.stop_progress_edits.assert_awaited_once_with(
            chat_id=12345,
            message_id=100,
        )
        server.application.bot.delete_message.assert_awaited_once_with(
            chat_id=12345,
            message_id=100,
        )
        receipt = (
            server.artifact_gateway.record_input_presentation_deletion.await_args
        )
        self.assertEqual(receipt.kwargs["generation"], 1)
        self.assertEqual(receipt.kwargs["deletion_state"], "deleted")

    async def test_first_event_binds_new_status_then_deletes_command_status(self):
        server = self._server()
        server.artifact_gateway.bind_input_presentation = AsyncMock(
            return_value={"state": "bound"}
        )
        submission = self._submission()
        submission["ack_policy"] = "create"
        submission["_telegram_previous_unbound_status_message_id"] = "100"

        result = await replace_unbound_collection_command_status(
            server=server,
            gateway=server.artifact_gateway,
            update=self._update(),
            submission=submission,
            session_id="telegram:conversation:12345",
        )

        self.assertEqual(result.message_id, 200)
        server.artifact_gateway.bind_input_presentation.assert_awaited_once_with(
            submission["presentation_ref"],
            session_id="telegram:conversation:12345",
            client_message_id="200",
        )
        server.stop_progress_edits.assert_awaited_once_with(
            chat_id=12345,
            message_id=100,
        )
        server.application.bot.delete_message.assert_awaited_once_with(
            chat_id=12345,
            message_id=100,
        )

    async def test_first_event_bind_failure_keeps_old_command_status(self):
        server = self._server()
        server.artifact_gateway.bind_input_presentation = AsyncMock(
            side_effect=RuntimeError("gateway unavailable")
        )
        submission = self._submission()
        submission["ack_policy"] = "create"
        submission["_telegram_previous_unbound_status_message_id"] = "100"

        result = await replace_unbound_collection_command_status(
            server=server,
            gateway=server.artifact_gateway,
            update=self._update(),
            submission=submission,
            session_id="telegram:conversation:12345",
        )

        self.assertEqual(result.message_id, 100)
        server.stop_progress_edits.assert_not_awaited()
        server.application.bot.delete_message.assert_awaited_once_with(
            chat_id=12345,
            message_id=200,
        )

    async def test_precreated_status_relocates_without_creating_another_message(self):
        server = self._server()
        status_message = SimpleNamespace(message_id=250, chat_id=12345)

        result = await relocate_precreated_input_presentation(
            server=server,
            gateway=server.artifact_gateway,
            submission=self._submission(),
            session_id="telegram:conversation:12345",
            status_message=status_message,
            cleanup_unbound=False,
        )

        self.assertIs(result, status_message)
        server.send_initial_status_message.assert_not_awaited()
        server.artifact_gateway.relocate_input_presentation.assert_awaited_once_with(
            self._submission()["presentation_ref"],
            session_id="telegram:conversation:12345",
            client_message_id="250",
        )
        server.application.bot.delete_message.assert_awaited_once_with(
            chat_id=12345,
            message_id=100,
        )
        server.artifact_gateway.record_input_presentation_deletion.assert_awaited_once()

    async def test_bind_failure_keeps_old_handle_and_cleans_only_new_message(self):
        server = self._server()
        server.artifact_gateway.relocate_input_presentation.side_effect = RuntimeError(
            "gateway unavailable"
        )

        result = await apply_relocating_input_ack_policy(
            base_apply=AsyncMock(),
            server=server,
            update=self._update(),
            submission=self._submission(),
            session_id="telegram:conversation:12345",
        )

        self.assertEqual(result.message_id, 100)
        server.stop_progress_edits.assert_not_awaited()
        server.application.bot.delete_message.assert_awaited_once_with(
            chat_id=12345,
            message_id=200,
        )
        server.artifact_gateway.record_input_presentation_deletion.assert_not_awaited()

    async def test_failed_old_delete_keeps_new_handle_authoritative(self):
        server = self._server()
        server.application.bot.delete_message.side_effect = BadRequest(
            "message cannot be deleted"
        )

        result = await apply_relocating_input_ack_policy(
            base_apply=AsyncMock(),
            server=server,
            update=self._update(),
            submission=self._submission(),
            session_id="telegram:conversation:12345",
        )

        self.assertEqual(result.message_id, 200)
        receipt = (
            server.artifact_gateway.record_input_presentation_deletion.await_args
        )
        self.assertEqual(receipt.kwargs["deletion_state"], "failed")

    async def test_non_relocation_policy_delegates_unchanged(self):
        server = self._server()
        expected = SimpleNamespace(message_id=55)
        base_apply = AsyncMock(return_value=expected)
        submission = self._submission()
        submission["ack_policy"] = "update_existing"

        result = await apply_relocating_input_ack_policy(
            base_apply=base_apply,
            server=server,
            update=self._update(),
            submission=submission,
            session_id="telegram:conversation:12345",
        )

        self.assertIs(result, expected)
        base_apply.assert_awaited_once()
        server.send_initial_status_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
