import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.ext import MessageHandler, filters

from src.servers.telegram.input_handler_policy import (
    route_semantic_or_attachment,
    wrap_telegram_attachment_handler,
)


async def _noop(update, context):
    return None


class TelegramInputHandlerPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_semantic_only_input_uses_shared_message_flow(self):
        update = SimpleNamespace(effective_message=object())
        context = object()
        attachment_handler = AsyncMock()
        semantic_handler = AsyncMock(return_value="semantic")

        result = await route_semantic_or_attachment(
            update,
            context,
            extract_attachments=lambda message: [],
            attachment_handler=attachment_handler,
            semantic_handler=semantic_handler,
        )

        self.assertEqual(result, "semantic")
        semantic_handler.assert_awaited_once_with(update, context)
        attachment_handler.assert_not_awaited()

    async def test_real_attachment_keeps_attachment_flow(self):
        update = SimpleNamespace(effective_message=object())
        context = object()
        attachment_handler = AsyncMock(return_value="attachment")
        semantic_handler = AsyncMock()

        result = await route_semantic_or_attachment(
            update,
            context,
            extract_attachments=lambda message: [object()],
            attachment_handler=attachment_handler,
            semantic_handler=semantic_handler,
        )

        self.assertEqual(result, "attachment")
        attachment_handler.assert_awaited_once_with(update, context)
        semantic_handler.assert_not_awaited()

    async def test_target_handler_is_wrapped_without_changing_filter_or_block(self):
        semantic_handler = AsyncMock(return_value="semantic")
        extract_attachments = lambda message: []

        async def attachment_handler(update, context):
            return "attachment"

        attachment_handler.__module__ = "src.servers.telegram.telegram_server"
        attachment_handler.__globals__["message_handler"] = semantic_handler
        attachment_handler.__globals__[
            "extract_telegram_attachments"
        ] = extract_attachments

        original = MessageHandler(
            filters.Document.ALL | filters.FORWARDED,
            attachment_handler,
            block=False,
        )
        wrapped = wrap_telegram_attachment_handler(original)

        self.assertIsInstance(wrapped, MessageHandler)
        self.assertIsNot(wrapped, original)
        self.assertEqual(wrapped.filters, original.filters)
        self.assertFalse(wrapped.block)
        self.assertIs(
            getattr(wrapped.callback, "_telegram_original_callback"),
            attachment_handler,
        )

        update = SimpleNamespace(effective_message=object())
        result = await wrapped.callback(update, object())
        self.assertEqual(result, "semantic")
        semantic_handler.assert_awaited_once()

    def test_unrelated_handler_is_not_wrapped(self):
        handler = MessageHandler(filters.TEXT, _noop)
        self.assertIs(wrap_telegram_attachment_handler(handler), handler)


if __name__ == "__main__":
    unittest.main()
