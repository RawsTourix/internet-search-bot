"""Canonical Telegram webhook and READY-outbox composition root."""

from __future__ import annotations

from contextlib import asynccontextmanager
from functools import wraps

import httpx
from telegram import BotCommand
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import CommandHandler, MessageHandler, filters

from . import telegram_server as server
from .batch_commands import input_collection_command_handler
from .collection_bridge import ExplicitCollectionTelegramGatewayClient
from .config import (
    GATEWAY_URL,
    TELEGRAM_API_KEY,
    TELEGRAM_BOT_INSTANCE_ID,
    TELEGRAM_DELIVERY_SPOOL_MEMORY_BYTES,
    TELEGRAM_FORWARDED_TEXT_JOIN_WAIT_SECONDS,
    TELEGRAM_MEDIA_GROUP_TEXT_JOIN_WINDOW_SECONDS,
    TELEGRAM_READY_OUTBOX_BATCH_LIMIT,
    TELEGRAM_READY_OUTBOX_MINIMUM_AGE_SECONDS,
    TELEGRAM_READY_OUTBOX_POLL_SECONDS,
)
from .presentation_relocation import apply_relocating_input_ack_policy
from .progress_redirect import TelegramProgressRedirects
from .ready_outbox import TelegramReadyOutboxWorker
from .runtime_state import KeyedAsyncLockPool
from .scoped_output_executor import InstanceScopedTelegramOutputPlanExecutor


# One exact client-instance authority is shared by the ordinary synchronous
# response path, the recovery outbox worker and delivery-content streaming.
# Handler functions in telegram_server resolve these module globals at call
# time, so replacing them here does not duplicate ingress or Telegram state.
artifact_gateway = ExplicitCollectionTelegramGatewayClient(
    gateway_url=GATEWAY_URL,
    api_key=TELEGRAM_API_KEY,
    client_instance_id=TELEGRAM_BOT_INSTANCE_ID,
    delivery_spool_memory_bytes=TELEGRAM_DELIVERY_SPOOL_MEMORY_BYTES,
    media_group_activity=server.media_group_activity,
    input_text_join_window_seconds=(
        TELEGRAM_MEDIA_GROUP_TEXT_JOIN_WINDOW_SECONDS
    ),
    forwarded_text_join_wait_seconds=(
        TELEGRAM_FORWARDED_TEXT_JOIN_WAIT_SECONDS
    ),
)
telegram_output_executor = InstanceScopedTelegramOutputPlanExecutor()
server.artifact_gateway = artifact_gateway
server.telegram_output_executor = telegram_output_executor


# Immutable InputBatch response routes can outlive several public Telegram
# presentation generations. Resolve every stale callback target to the latest
# writable message, including the processing status created directly below
# /send.
progress_redirects = TelegramProgressRedirects()
_base_enqueue_progress_message = getattr(
    server,
    "_v04_base_enqueue_progress_message",
    server.enqueue_progress_message,
)
server._v04_base_enqueue_progress_message = _base_enqueue_progress_message


def _register_progress_redirect(
    *,
    chat_id: int,
    old_message_id: int,
    new_message_id: int,
) -> None:
    progress_redirects.register(
        chat_id=int(chat_id),
        old_message_id=int(old_message_id),
        new_message_id=int(new_message_id),
    )


def _enqueue_redirected_progress_message(
    *,
    chat_id: int,
    message_id: int,
    text: str,
):
    resolved = progress_redirects.resolve(
        chat_id=int(chat_id),
        message_id=int(message_id),
    )
    if resolved != int(message_id):
        server.logger.debug(
            "telegram_progress_target_redirected chat_id=%s old_message_id=%s "
            "new_message_id=%s",
            chat_id,
            message_id,
            resolved,
        )
    return _base_enqueue_progress_message(
        chat_id=int(chat_id),
        message_id=resolved,
        text=text,
    )


server.register_progress_redirect = _register_progress_redirect
server.enqueue_progress_message = _enqueue_redirected_progress_message


# Initial status creation is part of the public workflow, not an optional
# cosmetic side effect. A single Telegram timeout must not leave the entire
# AgentCycle without a visible tracking handle.
_base_send_initial_status_message = getattr(
    server,
    "_v04_base_send_initial_status_message",
    server.send_initial_status_message,
)
server._v04_base_send_initial_status_message = _base_send_initial_status_message


async def _retrying_initial_status_message(update, text: str):
    try:
        return await server.telegram_reply_with_retries(
            update,
            text,
            max_retries=3,
            base_delay=0.5,
        )
    except (TimedOut, NetworkError) as error:
        server.logger.warning(
            "Failed to send initial Telegram status after retries: %r",
            error,
        )
        return None


server.send_initial_status_message = _retrying_initial_status_message


# Explicit controls are handled before the legacy generic command bridge, so
# command text never becomes an InputBatch text part.
server.application.add_handler(
    CommandHandler(
        ["collect", "send", "cancel"],
        input_collection_command_handler,
    ),
    group=-1,
)


def _route_forwarded_text_through_text_handler() -> None:
    """Forwarding is provenance, not an attachment media type."""

    narrowed = server.attachment_filter & ~(
        filters.FORWARDED & filters.TEXT
    )
    for handlers in server.application.handlers.values():
        for handler in handlers:
            if not isinstance(handler, MessageHandler):
                continue
            callback_name = getattr(handler.callback, "__name__", "")
            if (
                handler.filters is server.attachment_filter
                or callback_name == "attachment_handler"
            ):
                handler.filters = narrowed
                return
    raise RuntimeError("Telegram attachment handler is not registered")


_route_forwarded_text_through_text_handler()


# python-telegram-bot accepts webhook updates concurrently. Until CycleInbox is
# implemented, one exact Telegram session must have one FIFO admission/run lane;
# otherwise commands, new inputs and album completion can start overlapping
# AgentCycles and attach results to the wrong user message.
session_update_locks = KeyedAsyncLockPool()
_SERIALIZED_CALLBACKS = {
    "command_handler",
    "message_handler",
    "attachment_handler",
    "input_collection_command_handler",
}


def _serialized_handler(callback):
    if getattr(callback, "_v04_session_serialized", False):
        return callback

    @wraps(callback)
    async def wrapped(update, context):
        async with session_update_locks.hold(server._session_for_update(update)):
            return await callback(update, context)

    wrapped._v04_session_serialized = True
    return wrapped


def _install_session_serialization() -> None:
    for handlers in server.application.handlers.values():
        for handler in handlers:
            callback = getattr(handler, "callback", None)
            if callback is None:
                continue
            if getattr(callback, "__name__", "") not in _SERIALIZED_CALLBACKS:
                continue
            handler.callback = _serialized_handler(callback)


_install_session_serialization()
_base_finish_group = getattr(
    server,
    "_v04_base_finish_group",
    server._finish_group,
)
server._v04_base_finish_group = _base_finish_group


async def _serialized_finish_group(group):
    async with session_update_locks.hold(group.session_id):
        return await _base_finish_group(group)


server._finish_group = _serialized_finish_group


# Preserve the original transport-neutral acknowledgement executor across test
# re-imports. The wrapper only intercepts the explicit RELOCATE policy.
_base_apply_input_ack_policy = getattr(
    server,
    "_v04_base_apply_input_ack_policy",
    server.apply_input_ack_policy,
)
server._v04_base_apply_input_ack_policy = _base_apply_input_ack_policy


async def _remember_presentation_handle(submission, status_message) -> None:
    message_id = getattr(status_message, "message_id", None)
    if message_id is None:
        return
    await artifact_gateway.remember_input_presentation_handle(
        submission,
        client_message_id=str(message_id),
    )


async def _apply_input_ack_policy(*, update, submission, session_id):
    status_message = await apply_relocating_input_ack_policy(
        base_apply=_base_apply_input_ack_policy,
        server=server,
        update=update,
        submission=submission,
        session_id=session_id,
    )
    await _remember_presentation_handle(submission, status_message)
    return status_message


server.apply_input_ack_policy = _apply_input_ack_policy


# Media/standalone paths bind presentations directly rather than through the
# acknowledgement dispatcher, so keep the same latest-handle cache there too.
_base_bind_input_presentation_status = getattr(
    server,
    "_v04_base_bind_input_presentation_status",
    server.bind_input_presentation_status,
)
server._v04_base_bind_input_presentation_status = (
    _base_bind_input_presentation_status
)


async def _bind_input_presentation_status(*, submission, status_message, session_id):
    await _base_bind_input_presentation_status(
        submission=submission,
        status_message=status_message,
        session_id=session_id,
    )
    await _remember_presentation_handle(submission, status_message)


server.bind_input_presentation_status = _bind_input_presentation_status


async def _retire_input_collection_status(
    *,
    update,
    previous_message_id,
    processing_status_message,
) -> None:
    """Move run progress below /send and retire the closed collection status."""

    new_message_id = getattr(processing_status_message, "message_id", None)
    if previous_message_id is None or new_message_id is None:
        return
    try:
        old_message_id = int(previous_message_id)
        new_message_id = int(new_message_id)
    except (TypeError, ValueError):
        return
    if old_message_id == new_message_id:
        return

    _register_progress_redirect(
        chat_id=int(update.effective_chat.id),
        old_message_id=old_message_id,
        new_message_id=new_message_id,
    )
    await server.stop_progress_edits(
        chat_id=update.effective_chat.id,
        message_id=old_message_id,
    )
    try:
        await server.application.bot.delete_message(
            chat_id=update.effective_chat.id,
            message_id=old_message_id,
        )
        deletion_state = "deleted"
    except BadRequest:
        deletion_state = "failed"
    except (TimedOut, NetworkError):
        deletion_state = "unknown"
    except Exception:
        deletion_state = "failed"
    server.logger.info(
        "telegram_collection_status_retired old_message_id=%s "
        "processing_message_id=%s deletion_state=%s",
        old_message_id,
        new_message_id,
        deletion_state,
    )


server.retire_input_collection_status = _retire_input_collection_status


ready_outbox_worker = TelegramReadyOutboxWorker(
    gateway_url=GATEWAY_URL,
    api_key=TELEGRAM_API_KEY,
    client_instance_id=TELEGRAM_BOT_INSTANCE_ID,
    bot=server.application.bot,
    gateway=artifact_gateway,
    executor=telegram_output_executor,
    poll_seconds=TELEGRAM_READY_OUTBOX_POLL_SECONDS,
    minimum_age_seconds=TELEGRAM_READY_OUTBOX_MINIMUM_AGE_SECONDS,
    batch_limit=TELEGRAM_READY_OUTBOX_BATCH_LIMIT,
)


_base_deliver_agent_result = server._deliver_agent_result


async def _strict_markdown_reply(update, text: str):
    """Do not swallow an exhausted Telegram transport retry sequence."""

    sent = []
    for markdown_chunk in server.split_markdown_for_telegram(text or ""):
        try:
            message = await server.telegram_reply_with_retries(
                update,
                server.markdown_to_telegram_html(markdown_chunk),
                parse_mode=ParseMode.HTML,
            )
        except BadRequest:
            message = await server.telegram_reply_with_retries(
                update,
                server.markdown_to_plain_text(markdown_chunk),
                parse_mode=None,
            )
        sent.append(message)
    return sent


# Keep the public low-level seam authoritative for tests and compatibility
# callers, while allowing tests focused on this composition root to patch the
# private strict implementation directly. The selector below supports either
# patch point without changing runtime behavior.
_installed_strict_markdown_reply = _strict_markdown_reply
server.send_telegram_markdown_reply = _installed_strict_markdown_reply


def _terminal_reply_sender():
    public_sender = server.send_telegram_markdown_reply
    if public_sender is _installed_strict_markdown_reply:
        return _strict_markdown_reply
    return public_sender


async def _edit_known_status(update, status_message, text: str):
    chunks = server.split_markdown_for_telegram(text or "")
    chunk = chunks[0] if chunks else (text or "")
    if len(chunks) > 1:
        chunk = chunk.rstrip() + "\n\n…"
    try:
        await server.edit_telegram_message_with_retries(
            chat_id=update.effective_chat.id,
            message_id=status_message.message_id,
            text=server.markdown_to_telegram_html(chunk),
            parse_mode=ParseMode.HTML,
        )
    except BadRequest:
        await server.edit_telegram_message_with_retries(
            chat_id=update.effective_chat.id,
            message_id=status_message.message_id,
            text=server.markdown_to_plain_text(chunk),
            parse_mode=None,
        )
    return status_message


async def _finish_status_or_send_reply(
    *,
    update,
    status_message,
    text: str,
    force_reply_if_long: bool = False,
    delivery_mode: str | None = None,
):
    """Deliver terminal text without repeating an ambiguous send-new action."""

    mode = (
        delivery_mode or server.TELEGRAM_FINAL_DELIVERY_MODE
    ).lower().strip()
    if mode not in {"send_new", "edit_status", "auto"}:
        mode = "send_new"
    if status_message is None:
        return await _terminal_reply_sender()(update, text)

    await server.stop_progress_edits(
        chat_id=update.effective_chat.id,
        message_id=status_message.message_id,
    )
    raw = text or ""
    chunks = server.split_markdown_for_telegram(raw)
    send_new = (
        mode == "send_new"
        or force_reply_if_long
        or len(raw) > server.TELEGRAM_FINAL_EDIT_MAX_LENGTH
        or len(chunks) != 1
    )

    if send_new:
        try:
            return await _terminal_reply_sender()(update, raw)
        except (TimedOut, NetworkError) as error:
            await _edit_known_status(update, status_message, raw)
            server.logger.warning(
                "telegram_terminal_text_fell_back_to_status "
                "chat_id=%s message_id=%s original_error_type=%s",
                update.effective_chat.id,
                status_message.message_id,
                type(error).__name__,
            )
            return status_message

    try:
        return await _edit_known_status(update, status_message, raw)
    except (TimedOut, NetworkError):
        return await _terminal_reply_sender()(update, raw)


async def _deliver_agent_result(**values):
    """Handle explicit collection status and rare synchronous/outbox races."""

    metadata = dict(values.get("metadata") or {})
    if metadata.get("input_collection_terminal_suppressed"):
        status_message = values.get("status_message")
        if status_message is not None:
            await server.stop_progress_edits(
                chat_id=values["update"].effective_chat.id,
                message_id=status_message.message_id,
            )
        server.logger.info(
            "telegram_late_collection_group_suppressed input_batch_action=%s",
            metadata.get("terminal_action"),
        )
        return None

    if metadata.get("input_collection_pending"):
        locale = str(metadata.get("progress_locale") or "ru")
        return await server.finish_status_or_send_reply(
            update=values["update"],
            status_message=values.get("status_message"),
            text=server._localized(
                "input_collection.collecting",
                locale=locale,
                file_count=int(metadata.get("file_count") or 0),
                text_part_count=int(metadata.get("text_part_count") or 0),
            ),
            delivery_mode="edit_status",
        )

    try:
        return await _base_deliver_agent_result(**values)
    except httpx.HTTPStatusError as error:
        request = error.request
        if (
            error.response.status_code == 409
            and request.method == "POST"
            and request.url.path.endswith("/claim")
            and "/internal/output-outbox/" in request.url.path
        ):
            output_batch = metadata.get("output_batch") or {}
            server.logger.info(
                "telegram_output_claim_already_owned output_batch_id=%s",
                output_batch.get("output_batch_id"),
            )
            return None
        raise


server.finish_status_or_send_reply = _finish_status_or_send_reply
server._deliver_agent_result = _deliver_agent_result


@asynccontextmanager
async def lifespan(app):
    # The original lifecycle owns python-telegram-bot and webhook setup. The
    # outbox worker starts only after that setup has completed and stops before
    # the bot application is shut down. This composition owns exactly one
    # worker and one Telegram Application instance per process.
    async with server.lifespan(app):
        await server.application.bot.set_my_commands([
            BotCommand("start", "Приветствие"),
            BotCommand("status", "Статус системы"),
            BotCommand("collect", "Начать сбор пакета"),
            BotCommand("send", "Отправить собранный пакет"),
            BotCommand("cancel", "Отменить сбор пакета"),
            BotCommand("reset", "Очистка памяти"),
            BotCommand("help", "Справка"),
        ])
        await ready_outbox_worker.start()
        app.state.telegram_ready_outbox = ready_outbox_worker
        try:
            yield
        finally:
            await ready_outbox_worker.stop()


server.app.router.lifespan_context = lifespan
app = server.app
