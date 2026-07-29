"""Canonical Telegram webhook and READY-outbox composition root."""

from __future__ import annotations

from contextlib import asynccontextmanager

import httpx
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, TimedOut

from . import telegram_server as server
from .config import (
    GATEWAY_URL,
    TELEGRAM_API_KEY,
    TELEGRAM_BOT_INSTANCE_ID,
    TELEGRAM_DELIVERY_SPOOL_MEMORY_BYTES,
    TELEGRAM_MEDIA_GROUP_TEXT_JOIN_WINDOW_SECONDS,
    TELEGRAM_READY_OUTBOX_BATCH_LIMIT,
    TELEGRAM_READY_OUTBOX_MINIMUM_AGE_SECONDS,
    TELEGRAM_READY_OUTBOX_POLL_SECONDS,
)
from .ready_outbox import TelegramReadyOutboxWorker
from .scoped_artifact_bridge import InstanceScopedTelegramArtifactGatewayClient
from .scoped_output_executor import InstanceScopedTelegramOutputPlanExecutor


# One exact client-instance authority is shared by the ordinary synchronous
# response path, the recovery outbox worker and delivery-content streaming.
# Handler functions in telegram_server resolve these module globals at call
# time, so replacing them here does not duplicate ingress or Telegram state.
artifact_gateway = InstanceScopedTelegramArtifactGatewayClient(
    gateway_url=GATEWAY_URL,
    api_key=TELEGRAM_API_KEY,
    client_instance_id=TELEGRAM_BOT_INSTANCE_ID,
    delivery_spool_memory_bytes=TELEGRAM_DELIVERY_SPOOL_MEMORY_BYTES,
    media_group_activity=server.media_group_activity,
    input_text_join_window_seconds=(
        TELEGRAM_MEDIA_GROUP_TEXT_JOIN_WINDOW_SECONDS
    ),
)
telegram_output_executor = InstanceScopedTelegramOutputPlanExecutor()
server.artifact_gateway = artifact_gateway
server.telegram_output_executor = telegram_output_executor


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
_base_finish_status_or_send_reply = server.finish_status_or_send_reply


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


async def _finish_status_or_send_reply(**values):
    """Use a known status message after an ambiguous send-new timeout.

    A timeout after ``reply_text`` may mean the new message was accepted, so the
    same non-idempotent send is not repeated again. Updating the already known
    status message is a separate deterministic operation and guarantees that a
    terminal error remains visible even when the direct final send is uncertain.
    """

    original_sender = server.send_telegram_markdown_reply
    server.send_telegram_markdown_reply = _strict_markdown_reply
    try:
        return await _base_finish_status_or_send_reply(**values)
    except (TimedOut, NetworkError) as error:
        status_message = values.get("status_message")
        update = values.get("update")
        text = str(values.get("text") or "")
        if status_message is None or update is None:
            server.logger.error(
                "telegram_terminal_text_delivery_unknown "
                "status_message_available=false error_type=%s",
                type(error).__name__,
            )
            raise

        chunks = server.split_markdown_for_telegram(text)
        chunk = chunks[0] if chunks else text
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
        server.logger.warning(
            "telegram_terminal_text_fell_back_to_status "
            "chat_id=%s message_id=%s original_error_type=%s",
            update.effective_chat.id,
            status_message.message_id,
            type(error).__name__,
        )
        return status_message
    finally:
        server.send_telegram_markdown_reply = original_sender


async def _deliver_agent_result(**values):
    """Let the durable worker win a rare synchronous/outbox claim race."""

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
            output_batch = (values.get("metadata") or {}).get("output_batch") or {}
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
        await ready_outbox_worker.start()
        app.state.telegram_ready_outbox = ready_outbox_worker
        try:
            yield
        finally:
            await ready_outbox_worker.stop()


server.app.router.lifespan_context = lifespan
app = server.app
