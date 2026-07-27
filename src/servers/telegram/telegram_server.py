"""Telegram process entrypoint with v0.4 durable output-outbox recovery."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from . import telegram_runtime as _runtime
from .config import (
    TELEGRAM_API_KEY,
    TELEGRAM_BOT_INSTANCE_ID,
    TELEGRAM_READY_OUTBOX_BATCH_LIMIT,
    TELEGRAM_READY_OUTBOX_MINIMUM_AGE_SECONDS,
    TELEGRAM_READY_OUTBOX_POLL_SECONDS,
)
from .ready_outbox import TelegramReadyOutboxWorker
from .scoped_output_executor import InstanceScopedTelegramOutputPlanExecutor


# Both the synchronous response path and the recovery worker use the same
# OutputBatch-scoped executor. Replacing the runtime composition object preserves
# all existing handlers while removing legacy per-delivery byte access.
_runtime.telegram_output_executor = InstanceScopedTelegramOutputPlanExecutor()

ready_output_outbox = TelegramReadyOutboxWorker(
    gateway_url=_runtime.GATEWAY_URL,
    api_key=TELEGRAM_API_KEY,
    client_instance_id=TELEGRAM_BOT_INSTANCE_ID,
    bot=_runtime.application.bot,
    gateway=_runtime.artifact_gateway,
    executor=_runtime.telegram_output_executor,
    poll_seconds=TELEGRAM_READY_OUTBOX_POLL_SECONDS,
    minimum_age_seconds=TELEGRAM_READY_OUTBOX_MINIMUM_AGE_SECONDS,
    batch_limit=TELEGRAM_READY_OUTBOX_BATCH_LIMIT,
)

_original_lifespan = _runtime.app.router.lifespan_context


@asynccontextmanager
async def lifespan(app):
    """Run the transport worker only while the Telegram service is alive."""

    async with _original_lifespan(app):
        await ready_output_outbox.start()
        try:
            yield
        finally:
            await ready_output_outbox.stop()


_runtime.app.router.lifespan_context = lifespan
app = _runtime.app
application = _runtime.application
artifact_gateway = _runtime.artifact_gateway
telegram_output_executor = _runtime.telegram_output_executor


@app.get("/internal/ready-output-outbox/health")
async def ready_output_outbox_health() -> dict[str, Any]:
    return {
        "running": ready_output_outbox.running,
        "client_instance_id": TELEGRAM_BOT_INSTANCE_ID,
        "completed_count": ready_output_outbox.completed_count,
        "last_success_at": (
            ready_output_outbox.last_success_at.isoformat()
            if ready_output_outbox.last_success_at is not None
            else None
        ),
        "last_error_type": ready_output_outbox.last_error_type,
    }


def __getattr__(name: str) -> Any:
    """Preserve existing imports while runtime implementation lives separately."""

    return getattr(_runtime, name)
