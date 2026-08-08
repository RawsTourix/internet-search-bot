"""Thin compatibility marker for suspended-cycle committed batches."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any


_suspended_cycle_batch_continuation: ContextVar[str | None] = ContextVar(
    "suspended_cycle_batch_continuation",
    default=None,
)

_RESUMABLE_CYCLE_STATUSES = frozenset({"waiting_user", "interrupted"})


class WaitingUserBatchContinuationMixin:
    """Keep callers compatible while CP-RESUME owns semantic FIFO apply.

    A suspended-cycle batch is already durable in the input-runtime stores. It
    must not travel through the legacy query/input_batch conversion, which
    would append a direct reply and activate only that batch's artifacts.
    """

    async def process_query(self, *args: Any, **kwargs: Any):
        input_batch = kwargs.get("input_batch")
        session_id = str(kwargs.get("session_id") or "default")
        pending_cycle = None
        if input_batch is not None:
            pending_cycle = self._get_or_create_session(session_id).pending_cycle
        pending_status = str(getattr(pending_cycle, "status", ""))
        continuation_status = (
            pending_status
            if (
                input_batch is not None
                and pending_cycle is not None
                and pending_status in _RESUMABLE_CYCLE_STATUSES
            )
            else None
        )

        forwarded_args = args
        forwarded_kwargs = dict(kwargs)
        if continuation_status is not None:
            forwarded_kwargs.pop("input_batch", None)
            if args:
                forwarded_args = ("", *args[1:])
            else:
                forwarded_kwargs["query"] = ""

        token = _suspended_cycle_batch_continuation.set(continuation_status)
        try:
            return await super().process_query(
                *forwarded_args,
                **forwarded_kwargs,
            )
        finally:
            _suspended_cycle_batch_continuation.reset(token)
