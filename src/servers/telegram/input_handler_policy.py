"""Telegram handler routing for semantic-only and binary attachment inputs."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from telegram.ext import MessageHandler


AsyncHandler = Callable[[Any, Any], Awaitable[Any]]


async def route_semantic_or_attachment(
    update: Any,
    context: Any,
    *,
    extract_attachments: Callable[[Any], Any],
    attachment_handler: AsyncHandler,
    semantic_handler: AsyncHandler,
) -> Any:
    """Route only real binary attachments through standalone/group commit logic.

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


def replace_attachment_handler(
    application: Any,
    *,
    original_callback: AsyncHandler,
    replacement_callback: AsyncHandler,
) -> None:
    """Replace one registered PTB MessageHandler without changing its order.

    Handler order is significant in python-telegram-bot because only the first
    matching handler in a group is executed. Replacement therefore happens in
    place before the Application is initialized rather than removing and
    appending the handler at the end of the group.
    """

    matches: list[tuple[int, int, MessageHandler]] = []
    for group, handlers in application.handlers.items():
        for index, handler in enumerate(handlers):
            if (
                isinstance(handler, MessageHandler)
                and handler.callback is original_callback
            ):
                matches.append((group, index, handler))

    if len(matches) != 1:
        raise RuntimeError(
            "Telegram attachment handler registration must have exactly one "
            f"match, found {len(matches)}"
        )

    group, index, existing = matches[0]
    application.handlers[group][index] = MessageHandler(
        existing.filters,
        replacement_callback,
        block=existing.block,
    )
