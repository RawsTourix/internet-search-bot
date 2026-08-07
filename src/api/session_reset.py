"""Session reset coordinated by the IR-5 durable control plane."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from uuid import uuid4


logger = logging.getLogger("API.SessionReset")


@dataclass(frozen=True, slots=True)
class SessionResetResult:
    session_id: str
    cancelled_input_batch_ids: tuple[str, ...]
    control_id: str | None = None
    generation: int | None = None

    @property
    def cancelled_input_batch_count(self) -> int:
        return len(self.cancelled_input_batch_ids)


def _reset_application_lock(api, session_id: str) -> asyncio.Lock:
    locks = getattr(api, "_input_runtime_reset_application_locks", None)
    if locks is None:
        locks = {}
        setattr(api, "_input_runtime_reset_application_locks", locks)
    return locks.setdefault(session_id, asyncio.Lock())


def _completed_reset_controls(api) -> set[str]:
    completed = getattr(api, "_input_runtime_completed_reset_controls", None)
    if completed is None:
        completed = set()
        setattr(api, "_input_runtime_completed_reset_controls", completed)
    return completed


async def reset_runtime_session(
    api,
    session_id: str,
    *,
    idempotency_key: str | None = None,
    source_client_type: str = "application",
    source_message_ref: dict | None = None,
) -> SessionResetResult:
    """Advance durable generation, then clear mutable memory at a safe lease.

    The coordinator only mirrors the already-durable generation.  Open ingress
    drafts and shared MCP/session memory are cleared after the old runner leaves
    its current atomic block and releases the in-process execution lease.
    """

    execution_coordinator = getattr(api, "execution_coordinator", None)
    control_service = getattr(
        getattr(api, "input_admission_service", None),
        "control_service",
        None,
    )
    control_id: str | None = None
    durable_generation: int | None = None

    if control_service is not None:
        outcome = await control_service.request_reset(
            session_id=session_id,
            idempotency_key=(
                idempotency_key
                or f"application-reset:{session_id}:{uuid4().hex}"
            ),
            source_client_type=source_client_type,
            source_message_ref=source_message_ref,
            reason="user_reset",
        )
        control_id = outcome.command.control_id
        state = await api.input_runtime_repositories.sessions.get(session_id)
        durable_generation = state.generation if state is not None else None
        if execution_coordinator is not None and durable_generation is not None:
            await execution_coordinator.synchronize_generation(
                session_id,
                generation=durable_generation,
            )
    elif execution_coordinator is not None:
        # Compatibility only for a composition without the IR-5 service.
        durable_generation = await execution_coordinator.reset_session(session_id)

    async with _reset_application_lock(api, session_id):
        completed = _completed_reset_controls(api)
        if control_id is not None and control_id in completed:
            return SessionResetResult(
                session_id=session_id,
                cancelled_input_batch_ids=(),
                control_id=control_id,
                generation=durable_generation,
            )

        cancelled = []
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
            # Never mutate shared memory underneath an old-generation runner.
            # Durable generation fencing makes its next safe checkpoint unwind;
            # this lease only waits for that bounded cooperative boundary.
            async with execution_coordinator.run_lease(session_id=session_id):
                api.mcp_client.clear_session(session_id)

        if control_id is not None:
            completed.add(control_id)

    result = SessionResetResult(
        session_id=session_id,
        cancelled_input_batch_ids=tuple(
            draft.input_batch_id for draft in cancelled
        ),
        control_id=control_id,
        generation=durable_generation,
    )
    logger.info(
        "session_reset_completed session_id=%s generation=%s control_id=%s "
        "cancelled_input_batches=%s",
        session_id,
        durable_generation,
        control_id,
        result.cancelled_input_batch_count,
    )
    return result
