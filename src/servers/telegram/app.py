"""Canonical Telegram webhook and READY-outbox composition root."""

from __future__ import annotations

from contextlib import asynccontextmanager

from . import telegram_server as server
from .config import (
    GATEWAY_URL,
    TELEGRAM_API_KEY,
    TELEGRAM_BOT_INSTANCE_ID,
    TELEGRAM_DELIVERY_SPOOL_MEMORY_BYTES,
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
