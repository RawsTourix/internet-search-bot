"""Canonical Telegram webhook and READY-outbox composition root."""

from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
from types import SimpleNamespace
from typing import Any

import httpx
from telegram import BotCommand
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import CommandHandler, MessageHandler, filters

from ...agent.progress_messages import progress_text
from . import telegram_server as server
from .batch_commands import input_collection_command_handler
from .config import (
    GATEWAY_URL,
    TELEGRAM_API_KEY,
    TELEGRAM_BOT_INSTANCE_ID,
    TELEGRAM_DELIVERY_SPOOL_MEMORY_BYTES,
    TELEGRAM_FINAL_STATUS_MODE,
    TELEGRAM_FORWARDED_TEXT_JOIN_WAIT_SECONDS,
    TELEGRAM_MEDIA_GROUP_TEXT_JOIN_WINDOW_SECONDS,
    TELEGRAM_READY_OUTBOX_BATCH_LIMIT,
    TELEGRAM_READY_OUTBOX_MINIMUM_AGE_SECONDS,
    TELEGRAM_READY_OUTBOX_POLL_SECONDS,
)
from .presentation_relocation import (
    apply_relocating_input_ack_policy,
    replace_unbound_collection_command_status,
)
from .ready_outbox import TelegramReadyOutboxWorker
from .run_progress_bridge import RunScopedProgressTelegramGatewayClient
from .runtime_control_handlers import install_runtime_control_handlers
from .runtime_state import TelegramSessionDispatcher
from .scoped_output_executor import InstanceScopedTelegramOutputPlanExecutor


_ARTIFACT_OUTPUT_TYPES = frozenset({
    "artifact_output",
    "image_output",
    "audio_output",
    "voice_output",
    "video_output",
    "video_note_output",
    "animation_output",
    "sticker_output",
})
_output_completion_context: ContextVar[dict[str, Any] | None] = ContextVar(
    "telegram_output_completion_context",
    default=None,
)


artifact_gateway = RunScopedProgressTelegramGatewayClient(
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


install_runtime_control_handlers(server.application)
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


# A media-group tombstone is useful only if it stops admission. The base
# activity coordinator intentionally remains transport-neutral, while this
# Telegram composition checks the exact album key before creating any visible
# status message or making an ingress HTTP request.
_base_attachment_handler = getattr(
    server,
    "_v04_base_attachment_handler",
    server.attachment_handler,
)
server._v04_base_attachment_handler = _base_attachment_handler


async def _guarded_attachment_handler(update, context):
    message = update.effective_message
    media_group_id = getattr(message, "media_group_id", None)
    if media_group_id is not None:
        thread_id = getattr(message, "message_thread_id", None)
        group_key = (
            f"{TELEGRAM_BOT_INSTANCE_ID}:{update.effective_chat.id}:"
            f"{thread_id or '-'}:{media_group_id}"
        )
        is_closed = getattr(server.media_group_runner, "is_closed", None)
        if callable(is_closed) and await is_closed(group_key):
            server.logger.info(
                "telegram_media_group_late_update_suppressed "
                "group_key=%s message_id=%s",
                group_key,
                getattr(message, "message_id", None),
            )
            return None
    return await _base_attachment_handler(update, context)


server.attachment_handler = _guarded_attachment_handler
for _handlers in server.application.handlers.values():
    for _handler in _handlers:
        if not isinstance(_handler, MessageHandler):
            continue
        if (
            _handler.callback is _base_attachment_handler
            or getattr(_handler.callback, "__name__", "")
            in {"attachment_handler", "_guarded_attachment_handler"}
        ):
            _handler.callback = _guarded_attachment_handler
            break


session_dispatcher = TelegramSessionDispatcher()
_base_process_update = getattr(
    server.application,
    "_v04_base_process_update",
    server.application.process_update,
)
server.application._v04_base_process_update = _base_process_update

_base_reset_process_local_session = server.reset_process_local_session


async def _reset_process_local_session(session_id: str) -> None:
    await session_dispatcher.reset_session(session_id)
    await _base_reset_process_local_session(session_id)


server.reset_process_local_session = _reset_process_local_session


def _queued_process_update(update):
    session_id = server._session_for_update(update)
    message = getattr(update, "effective_message", None)
    text = str(getattr(message, "text", "") or "").strip().lower()
    command = text.split(maxsplit=1)[0].split("@", 1)[0] if text else ""
    is_command = command.startswith("/")
    collection_command = command in {"/collect", "/send", "/cancel"}
    collection_active = getattr(
        artifact_gateway,
        "is_explicit_collection_active_now",
        lambda _session_id: False,
    )(session_id)
    # A collection command and every data update that can observe it share one
    # exact FIFO admission lane. Ordinary auto-mode messages remain concurrent
    # once no collection command is pending, so agent execution is not
    # serialized accidentally. Read-only /status and reset still bypass it.
    if (
        collection_command
        or (
            not is_command
            and (
                collection_active
                or session_dispatcher.has_pending(session_id)
            )
        )
    ):
        operation = lambda: _base_process_update(update)
        if collection_command:
            return session_dispatcher.submit(session_id, operation)
        return session_dispatcher.submit_shared(session_id, operation)
    return _base_process_update(update)


server.application.process_update = _queued_process_update


_base_finish_group = getattr(
    server,
    "_v04_base_finish_group",
    server._finish_group,
)
server._v04_base_finish_group = _base_finish_group


async def _queued_finish_group(group):
    return await _base_finish_group(group)


server._finish_group = _queued_finish_group


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


def _is_committed_text_only_submission(submission: dict[str, Any]) -> bool:
    event = dict(submission.get("presentation_event") or {})
    params = dict(event.get("params") or {})
    return (
        str(submission.get("status") or "") == "committed"
        and not bool(submission.get("duplicate"))
        and int(params.get("file_count") or 0) == 0
        and int(params.get("text_part_count") or 0) > 0
    )


def _message_received_text(update, submission: dict[str, Any]) -> str:
    event = dict(submission.get("presentation_event") or {})
    locale = str(
        event.get("locale") or server.detect_progress_locale(update) or "ru"
    ).lower()
    if locale.startswith("en"):
        return "Message received. Processing…"
    return "Сообщение принято. Обрабатываю…"


async def _remember_auto_run_presentation(
    *,
    update,
    submission,
    status_message,
) -> None:
    if (
        str(submission.get("status") or "") != "committed"
        or bool(submission.get("duplicate"))
    ):
        return
    batch_id = str(submission.get("input_batch_id") or "").strip()
    if not batch_id:
        return
    await artifact_gateway.remember_run_presentation(
        batch_id,
        progress_metadata=server._progress_metadata(
            update,
            status_message,
            request_id=batch_id,
        ),
    )


async def _apply_input_ack_policy(*, update, submission, session_id):
    if (
        submission.get("_telegram_previous_unbound_status_message_id")
        is not None
    ):
        status_message = await replace_unbound_collection_command_status(
            server=server,
            gateway=artifact_gateway,
            update=update,
            submission=submission,
            session_id=session_id,
        )
    elif _is_committed_text_only_submission(submission):
        status_message = await server.send_initial_status_message(
            update,
            _message_received_text(update, submission),
        )
        await _base_bind_input_presentation_status(
            submission=submission,
            status_message=status_message,
            session_id=session_id,
        )
    else:
        status_message = await apply_relocating_input_ack_policy(
            base_apply=_base_apply_input_ack_policy,
            server=server,
            update=update,
            submission=submission,
            session_id=session_id,
        )
    await _remember_presentation_handle(submission, status_message)
    await server.adopt_input_batch_status(
        str(submission.get("input_batch_id") or ""),
        status_message,
    )
    await _remember_auto_run_presentation(
        update=update,
        submission=submission,
        status_message=status_message,
    )
    return status_message


server.apply_input_ack_policy = _apply_input_ack_policy


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


async def _finish_status_or_send_reply_core(
    *,
    update,
    status_message,
    text: str,
    force_reply_if_long: bool = False,
    delivery_mode: str | None = None,
):
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


def _completion_message_enabled(*, has_artifacts: bool) -> bool:
    return (
        TELEGRAM_FINAL_STATUS_MODE == "always"
        or (
            TELEGRAM_FINAL_STATUS_MODE == "artefacts_only"
            and has_artifacts
        )
    )


def _terminal_delivery_text(state: str, *, locale: str) -> str:
    if state == "delivered":
        return progress_text("cycle_done", locale_name=locale)
    key = {
        "partially_delivered": "output.delivery_incomplete",
        "failed": "output_batch.failed",
        "unknown": "output.delivery_unknown",
    }.get(state, "output.delivery_unknown")
    return server._localized(key, locale=locale)


async def _finish_status_or_send_reply(
    *,
    update,
    status_message,
    text: str,
    force_reply_if_long: bool = False,
    delivery_mode: str | None = None,
):
    completion = _output_completion_context.get()
    if completion is not None:
        take_state = getattr(
            server.artifact_gateway,
            "take_completed_output_state",
            None,
        )
        if callable(take_state):
            terminal_state = await take_state(
                str(completion["output_batch_id"])
            )
            if terminal_state:
                locale = str(completion["locale"])
                result = await _finish_status_or_send_reply_core(
                    update=update,
                    status_message=status_message,
                    text=_terminal_delivery_text(
                        terminal_state,
                        locale=locale,
                    ),
                    delivery_mode="edit_status",
                )
                if (
                    terminal_state == "delivered"
                    and _completion_message_enabled(
                        has_artifacts=bool(completion["has_artifacts"])
                    )
                ):
                    await _terminal_reply_sender()(
                        update,
                        server._localized("output.done", locale=locale),
                    )
                server.logger.info(
                    "telegram_run_status_finalized output_batch_id=%s "
                    "state=%s completion_message=%s",
                    completion["output_batch_id"],
                    terminal_state,
                    (
                        terminal_state == "delivered"
                        and _completion_message_enabled(
                            has_artifacts=bool(completion["has_artifacts"])
                        )
                    ),
                )
                return result

    return await _finish_status_or_send_reply_core(
        update=update,
        status_message=status_message,
        text=text,
        force_reply_if_long=force_reply_if_long,
        delivery_mode=delivery_mode,
    )


def _build_output_completion_context(
    values: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, Any] | None:
    if not bool(values.get("success")) or server.is_agent_error(metadata):
        return None
    output_batch = dict(metadata.get("output_batch") or {})
    output_batch_id = str(output_batch.get("output_batch_id") or "").strip()
    if not output_batch_id:
        return None
    parts = list(output_batch.get("parts") or [])
    has_artifacts = any(
        isinstance(part, dict)
        and str(part.get("type") or "") in _ARTIFACT_OUTPUT_TYPES
        for part in parts
    )
    return {
        "output_batch_id": output_batch_id,
        "has_artifacts": has_artifacts,
        "locale": server.normalize_locale(
            metadata.get("progress_locale") or "ru"
        ),
    }


async def _authoritative_collection_status_message(
    *,
    values: dict[str, Any],
    metadata: dict[str, Any],
):
    status_message = values.get("status_message")
    raw_message_id = metadata.get("presentation_message_id")
    try:
        authoritative_id = int(raw_message_id)
    except (TypeError, ValueError):
        return status_message

    current_id = getattr(status_message, "message_id", None)
    if current_id is not None and int(current_id) != authoritative_id:
        await server.stop_progress_edits(
            chat_id=values["update"].effective_chat.id,
            message_id=int(current_id),
        )
        server.logger.info(
            "telegram_collection_stale_status_redirected "
            "old_message_id=%s authoritative_message_id=%s",
            current_id,
            authoritative_id,
        )
    return SimpleNamespace(message_id=authoritative_id)


async def _deliver_agent_result(**values):
    metadata = dict(values.get("metadata") or {})
    response_generation = metadata.get("telegram_session_generation")
    if (
        response_generation is not None
        and not server.session_generations.is_current(
            values["session_id"],
            int(response_generation),
        )
    ):
        server.logger.info(
            "telegram_terminal_delivery_stale session_id=%s generation=%s",
            values["session_id"],
            response_generation,
        )
        return None
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
        async def deliver_collection_update():
            locale = str(metadata.get("progress_locale") or "ru")
            status_message = await _authoritative_collection_status_message(
                values=values,
                metadata=metadata,
            )
            return await server.finish_status_or_send_reply(
                update=values["update"],
                status_message=status_message,
                text=server._localized(
                    "input_collection.collecting",
                    locale=locale,
                    file_count=int(metadata.get("file_count") or 0),
                    text_part_count=int(
                        metadata.get("text_part_count") or 0
                    ),
                ),
                delivery_mode="edit_status",
            )

        input_batch_id = str(metadata.get("input_batch_id") or "").strip()
        presentation_guard = getattr(
            artifact_gateway,
            "explicit_presentation_guard",
            None,
        )
        if input_batch_id and callable(presentation_guard):
            async with presentation_guard(input_batch_id) as current:
                if current is not None and current.get("terminal"):
                    server.logger.info(
                        "telegram_stale_collection_update_suppressed "
                        "input_batch_id=%s terminal_action=%s",
                        input_batch_id,
                        current.get("action"),
                    )
                    return None
                if current is not None:
                    metadata["file_count"] = int(
                        current.get("file_count") or 0
                    )
                    metadata["text_part_count"] = int(
                        current.get("text_part_count") or 0
                    )
                    metadata["presentation_message_id"] = current.get(
                        "presentation_message_id"
                    )
                return await deliver_collection_update()
        return await deliver_collection_update()

    completion = _build_output_completion_context(values, metadata)
    token = (
        _output_completion_context.set(completion)
        if completion is not None
        else None
    )
    try:
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
    finally:
        if token is not None:
            _output_completion_context.reset(token)


server.finish_status_or_send_reply = _finish_status_or_send_reply
server._deliver_agent_result = _deliver_agent_result


@asynccontextmanager
async def lifespan(app):
    async with server.lifespan(app):
        await server.application.bot.set_my_commands([
            BotCommand("start", "Приветствие"),
            BotCommand("status", "Статус системы"),
            BotCommand("stop", "Приостановить текущую задачу"),
            BotCommand("continue", "Продолжить текущую задачу"),
            BotCommand("collect", "Начать сбор пакета"),
            BotCommand("send", "Отправить собранный пакет"),
            BotCommand("cancel", "Отменить сбор пакета"),
            BotCommand("reset", "Очистка памяти"),
            BotCommand("help", "Справка"),
        ])
        await ready_outbox_worker.start()
        app.state.telegram_ready_outbox = ready_outbox_worker
        app.state.telegram_session_dispatcher = session_dispatcher
        try:
            yield
        finally:
            await ready_outbox_worker.stop()
            await session_dispatcher.shutdown()


server.app.router.lifespan_context = lifespan
app = server.app
