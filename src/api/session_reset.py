"""Session-level reset across memory and uncommitted filesystem ingress state."""

from __future__ import annotations

import logging
from dataclasses import dataclass


logger = logging.getLogger("API.SessionReset")


@dataclass(frozen=True, slots=True)
class SessionResetResult:
    session_id: str
    cancelled_input_batch_ids: tuple[str, ...]

    @property
    def cancelled_input_batch_count(self) -> int:
        return len(self.cancelled_input_batch_ids)


async def reset_runtime_session(api, session_id: str) -> SessionResetResult:
    """Cancel open logical inputs, close presentations and clear LLM memory."""

    cancelled = []
    execution_coordinator = getattr(api, "execution_coordinator", None)
    if execution_coordinator is not None:
        await execution_coordinator.reset_session(session_id)
    batch_store = getattr(api.ingress_services, "batch_store", None)
    cancel_open = getattr(batch_store, "cancel_open_drafts", None)
    if cancel_open is not None:
        cancelled = await cancel_open(
            session_id=session_id,
            code="session_reset",
        )

    coordinator = getattr(
        api.ingress_services.ingress_service,
        "presentation_coordinator",
        None,
    )
    if coordinator is not None:
        for draft in cancelled:
            try:
                await coordinator.finalize_batch(
                    input_batch_id=draft.input_batch_id,
                    state="failed",
                    file_count=len(draft.attachment_parts),
                    text_part_count=len(draft.text_parts),
                    response_anchor=draft.response_anchor,
                )
            except Exception:
                logger.exception(
                    "session_reset_presentation_finalize_failed "
                    "session_id=%s input_batch_id=%s",
                    session_id,
                    draft.input_batch_id,
                )

    if execution_coordinator is None:
        api.mcp_client.clear_session(session_id)
    else:
        # Do not clear shared memory underneath an active AgentCycle. The
        # generation already rejected queued work; this lease waits only for
        # the current cycle's safe runtime boundary.
        async with execution_coordinator.run_lease(session_id=session_id):
            api.mcp_client.clear_session(session_id)
    result = SessionResetResult(
        session_id=session_id,
        cancelled_input_batch_ids=tuple(
            draft.input_batch_id for draft in cancelled
        ),
    )
    logger.info(
        "session_reset_completed session_id=%s cancelled_input_batches=%s",
        session_id,
        result.cancelled_input_batch_count,
    )
    return result
