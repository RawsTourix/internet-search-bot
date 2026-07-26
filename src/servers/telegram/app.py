"""Production composition root for Telegram webhook and READY outbox delivery."""

from __future__ import annotations

from contextlib import asynccontextmanager

from . import telegram_server as server
from .config import (
    GATEWAY_URL,
    TELEGRAM_API_KEY,
    TELEGRAM_BOT_INSTANCE_ID,
    TELEGRAM_READY_OUTBOX_BATCH_LIMIT,
    TELEGRAM_READY_OUTBOX_MINIMUM_AGE_SECONDS,
    TELEGRAM_READY_OUTBOX_POLL_SECONDS,
)
from .ready_outbox import TelegramReadyOutboxWorker


ready_outbox_worker = TelegramReadyOutboxWorker(
    gateway_url=GATEWAY_URL,
    api_key=TELEGRAM_API_KEY,
    client_instance_id=TELEGRAM_BOT_INSTANCE_ID,
    bot=server.application.bot,
    gateway=server.artifact_gateway,
    executor=server.telegram_output_executor,
    poll_seconds=TELEGRAM_READY_OUTBOX_POLL_SECONDS,
    minimum_age_seconds=TELEGRAM_READY_OUTBOX_MINIMUM_AGE_SECONDS,
    batch_limit=TELEGRAM_READY_OUTBOX_BATCH_LIMIT,
)


@asynccontextmanager
async def lifespan(app):
    # The original lifecycle owns python-telegram-bot and webhook setup. The
    # outbox worker starts only after that setup has completed and stops before
    # the bot application is shut down.
    async with server.lifespan(app):
        await ready_outbox_worker.start()
        app.state.telegram_ready_outbox = ready_outbox_worker
        try:
            yield
        finally:
            await ready_outbox_worker.stop()


server.app.router.lifespan_context = lifespan
app = server.app
