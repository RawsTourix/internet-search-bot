"""Telegram handler routing for semantic-only and binary attachment inputs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from telegram.ext import Application, MessageHandler


AsyncHandler = Callable[[Any, Any], Awaitable[Any]]
_INSTALLED = False
_ORIGINAL_ADD_HANDLER = Application.add_handler


async def route_semantic_or_attachment(
    update: Any,
    context: Any,
    *,
    extract_attachments: Callable[[Any], Any],
    attachment_handler: AsyncHandler,
    semantic_handler: AsyncHandler,
) -> Any:
    """Route only real binary attachments through grouped commit logic.

    Telegram's FORWARDED, LOCATION, CONTACT and POLL filters may match messages
    that contain semantic input but no transport attachment. Those events must
    use the shared semantic-message flow: Gateway may atomically commit them or
    join them to an already open attachment InputBatch. Treating them as a
    standalone attachment would issue a second client-side commit for a batch
    whose commit is owned by the media-group coordinator.
    """

    message = update.effective_message
    if extract_attachments(message):
        return await attachment_handler(update, context)
    return await semantic_handler(update, context)


def wrap_telegram_attachment_handler(handler: Any) -> Any:
    """Wrap only the low-level Telegram attachment MessageHandler.

    The function is intentionally pure so registration behavior can be tested
    without starting python-telegram-bot. Handler order, filter and ``block``
    semantics are preserved exactly.
    """

    if not isinstance(handler, MessageHandler):
        return handler
    callback = handler.callback
    if (
        getattr(callback, "__name__", "") != "attachment_handler"
        or not str(getattr(callback, "__module__", "")).endswith(
            ".telegram_server"
        )
    ):
        return handler

    callback_globals = callback.__globals__
    extract_attachments = callback_globals.get("extract_telegram_attachments")
    semantic_handler = callback_globals.get("message_handler")
    if not callable(extract_attachments) or not callable(semantic_handler):
        raise RuntimeError(
            "Telegram attachment handler lacks semantic routing dependencies"
        )

    async def semantic_or_attachment(update, context):
        return await route_semantic_or_attachment(
            update,
            context,
            extract_attachments=extract_attachments,
            attachment_handler=callback,
            semantic_handler=semantic_handler,
        )

    semantic_or_attachment.__name__ = "semantic_or_attachment_handler"
    semantic_or_attachment.__module__ = callback.__module__
    setattr(semantic_or_attachment, "_telegram_original_callback", callback)
    return MessageHandler(
        handler.filters,
        semantic_or_attachment,
        block=handler.block,
    )


def install_attachment_handler_registration_policy() -> None:
    """Install package-wide routing before telegram_server registers handlers.

    This keeps the canonical and low-level compatibility entrypoints behaviorally
    identical for input grouping, while READY-outbox polling remains exclusive
    to the canonical composition root.
    """

    global _INSTALLED
    if _INSTALLED:
        return

    def add_handler(application, handler, group=0):
        return _ORIGINAL_ADD_HANDLER(
            application,
            wrap_telegram_attachment_handler(handler),
            group,
        )

    Application.add_handler = add_handler
    _INSTALLED = True
