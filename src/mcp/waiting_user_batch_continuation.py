"""Allow one committed InputBatch to continue a suspended agent cycle."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from .artifact_request_context import get_artifact_request_input_batch


_suspended_cycle_batch_continuation: ContextVar[str | None] = ContextVar(
    "suspended_cycle_batch_continuation",
    default=None,
)

_RESUMABLE_CYCLE_STATUSES = frozenset({"waiting_user", "interrupted"})


class WaitingUserBatchContinuationMixin:
    """Treat the next committed package as a suspended cycle continuation.

    The current runtime cannot accept additions while an AgentCycle is running.
    A WAITING_USER or infrastructure-interrupted cycle is different: it is not
    executing, and the next committed package is the user's reply or resume
    signal.  This mixin marks that narrow admission path before the base client
    resumes the pending cycle, then lets the artifact layer add the new batch
    refs while preserving all refs already owned by the same cycle.
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
        previous_batch_id = getattr(
            active_cycle,
            "original_input_batch_id",
            None,
        )
        continuation_status = _suspended_cycle_batch_continuation.get()
        if (
            continuation_status is not None
            and input_batch is not None
            and previous_batch_id is not None
            and previous_batch_id != input_batch.input_batch_id
        ):
            # ArtifactDeliveryMixin rejects a different committed batch by
            # default. For a suspended cycle this exact package is the
            # continuation, not concurrent CycleInbox input. Preserve the old
            # artifact refs and admit the new batch through the inherited
            # normal activation path.
            active_cycle.original_input_batch_id = input_batch.input_batch_id
            event_type = (
                "waiting_user_input_batch_continued"
                if continuation_status == "waiting_user"
                else "interrupted_input_batch_continued"
            )
            self._trace_event(
                active_cycle.cycle_trace,
                event_type,
                previous_input_batch_id=previous_batch_id,
                input_batch_id=input_batch.input_batch_id,
                artifact_count=len(input_batch.artifact_refs),
                text_part_count=len(input_batch.text_parts),
            )

        return super()._activate_manager_context(
            active_cycle=active_cycle,
            state=state,
            session_id=session_id,
            progress_callback=progress_callback,
        )
