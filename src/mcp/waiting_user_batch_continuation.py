"""Thin compatibility marker for suspended-cycle committed batches."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from .artifact_request_context import (
    get_artifact_request_input_batch,
    reset_artifact_request_input_batch,
    set_artifact_request_input_batch,
)


_suspended_cycle_batch_continuation: ContextVar[str | None] = ContextVar(
    "suspended_cycle_batch_continuation",
    default=None,
)

_RESUMABLE_CYCLE_STATUSES = frozenset({"waiting_user", "interrupted"})


def is_suspended_batch_continuation() -> bool:
    return _suspended_cycle_batch_continuation.get() is not None


class WaitingUserBatchContinuationMixin:
    """Keep legacy callers working while IR-4 owns semantic FIFO apply.

    The compatibility layer only identifies a resume invocation.  It does not
    choose ordering, append the reply semantically, replace the initial batch
    identity, or activate its artifacts directly.  CP-RESUME removes the legacy
    envelope and CycleInputApplier owns all queued additions.
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
        token = _suspended_cycle_batch_continuation.set(continuation_status)
        try:
            return await super().process_query(*args, **kwargs)
        finally:
            _suspended_cycle_batch_continuation.reset(token)

    def _activate_manager_context(
        self,
        *,
        active_cycle,
        state,
        session_id: str,
        progress_callback,
    ):
        input_batch = get_artifact_request_input_batch()
        continuation_status = _suspended_cycle_batch_continuation.get()
        if continuation_status is None or input_batch is None:
            return super()._activate_manager_context(
                active_cycle=active_cycle,
                state=state,
                session_id=session_id,
                progress_callback=progress_callback,
            )

        batch_token = set_artifact_request_input_batch(None)
        try:
            return super()._activate_manager_context(
                active_cycle=active_cycle,
                state=state,
                session_id=session_id,
                progress_callback=progress_callback,
            )
        finally:
            reset_artifact_request_input_batch(batch_token)
