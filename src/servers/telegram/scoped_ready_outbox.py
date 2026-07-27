"""Backward-compatible alias for the instance-scoped READY outbox worker."""

from __future__ import annotations

from .ready_outbox import TelegramReadyOutboxWorker


class InstanceScopedTelegramReadyOutboxWorker(TelegramReadyOutboxWorker):
    """Compatibility name retained after claim idempotency moved into the base.

    The base worker now owns the stable claim-request ID and retry contract for
    every transport implementation. Keeping this alias avoids breaking imports
    while preventing nested retry loops and lost completion counts.
    """


__all__ = ["InstanceScopedTelegramReadyOutboxWorker"]
