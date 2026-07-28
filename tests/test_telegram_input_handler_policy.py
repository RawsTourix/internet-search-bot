import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.ext import MessageHandler, filters

from src.servers.telegram.input_handler_policy import (
    replace_attachment_handler,
    route_semantic_or_attachment,
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

    def test_registered_handler_is_replaced_in_place(self):
        before = MessageHandler(filters.COMMAND, _noop)
        original = MessageHandler(filters.ALL, _noop, block=False)
        after = MessageHandler(filters.TEXT, _noop)
        application = SimpleNamespace(handlers={0: [before, original, after]})

        async def replacement(update, context):
            return None

        replace_attachment_handler(
            application,
            original_callback=_noop,
            replacement_callback=replacement,
        )

        # _noop appears in all three handlers above, therefore exact callback
        # identity alone would be ambiguous. Build a unique callback case below.
        self.assertEqual(application.handlers[0], [before, original, after])

    def test_unique_registered_handler_preserves_filter_order_and_block(self):
        async def before_callback(update, context):
            return None

        async def attachment_callback(update, context):
            return None

        async def after_callback(update, context):
            return None

        async def replacement_callback(update, context):
            return None

        before = MessageHandler(filters.COMMAND, before_callback)
        original = MessageHandler(
            filters.Document.ALL | filters.FORWARDED,
            attachment_callback,
            block=False,
        )
        after = MessageHandler(filters.TEXT, after_callback)
        application = SimpleNamespace(handlers={0: [before, original, after]})

        replace_attachment_handler(
            application,
            original_callback=attachment_callback,
            replacement_callback=replacement_callback,
        )

        replaced = application.handlers[0][1]
        self.assertIs(application.handlers[0][0], before)
        self.assertIs(application.handlers[0][2], after)
        self.assertIsInstance(replaced, MessageHandler)
        self.assertIs(replaced.callback, replacement_callback)
        self.assertEqual(replaced.filters, original.filters)
        self.assertFalse(replaced.block)

    def test_missing_or_ambiguous_registration_is_rejected(self):
        async def target(update, context):
            return None

        with self.assertRaises(RuntimeError):
            replace_attachment_handler(
                SimpleNamespace(handlers={0: []}),
                original_callback=target,
                replacement_callback=_noop,
            )

        duplicate = MessageHandler(filters.ALL, target)
        with self.assertRaises(RuntimeError):
            replace_attachment_handler(
                SimpleNamespace(handlers={0: [duplicate, duplicate]}),
                original_callback=target,
                replacement_callback=_noop,
            )


if __name__ == "__main__":
    unittest.main()
