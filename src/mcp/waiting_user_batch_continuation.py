"""Allow one committed InputBatch to continue a suspended WAITING_USER cycle."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from .artifact_request_context import get_artifact_request_input_batch


_waiting_user_batch_continuation: ContextVar[bool] = ContextVar(
    "waiting_user_batch_continuation",
    default=False,
)


class WaitingUserBatchContinuationMixin:
    """Treat `/collect ... /send` as a normal reply to WAITING_USER.

    The current runtime cannot accept additions while an AgentCycle is running.
    A suspended WAITING_USER cycle is different: it is not executing, and the
    next committed package is the user's reply.  This mixin marks that narrow
    admission path before the base client resumes the pending cycle, then lets
    the artifact layer add the new batch refs while preserving all refs already
    owned by the same cycle.
    """

    async def process_query(self, *args: Any, **kwargs: Any):
        input_batch = kwargs.get("input_batch")
        session_id = str(kwargs.get("session_id") or "default")
        pending_cycle = None
        if input_batch is not None:
            pending_cycle = self._get_or_create_session(session_id).pending_cycle
        is_continuation = bool(
            input_batch is not None
            and pending_cycle is not None
            and str(getattr(pending_cycle, "status", "")) == "waiting_user"
        )
        token = _waiting_user_batch_continuation.set(is_continuation)
        try:
            return await super().process_query(*args, **kwargs)
        finally:
            _waiting_user_batch_continuation.reset(token)

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
        if (
            _waiting_user_batch_continuation.get()
            and input_batch is not None
            and previous_batch_id is not None
            and previous_batch_id != input_batch.input_batch_id
        ):
            # ArtifactDeliveryMixin rejects a different committed batch by
            # default. For a suspended WAITING_USER cycle this exact package is
            # the continuation, not concurrent CycleInbox input. Preserve the
            # old artifact refs and admit the new batch through the inherited
            # normal activation path.
            active_cycle.original_input_batch_id = input_batch.input_batch_id
            self._trace_event(
                active_cycle.cycle_trace,
                "waiting_user_input_batch_continued",
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
