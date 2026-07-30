"""Explicit user boundary between a waiting cycle and a new collected task."""

from __future__ import annotations


class FreshTaskBoundaryMixin:
    """Allow transports to abandon WAITING_USER before admitting a new task.

    A normal follow-up message still resumes ``pending_cycle``.  Starting an
    explicit collection is different: the user has opened a new input package,
    so its later committed InputBatch must start a fresh AgentCycle until the
    durable CycleInbox runtime exists.
    """

    def abandon_pending_cycle_for_new_task(
        self,
        session_id: str,
        *,
        reason: str,
    ) -> str | None:
        session = self._get_or_create_session(session_id)
        pending = session.pending_cycle
        if pending is None:
            return None

        normalized_reason = reason.strip() or "explicit_new_task"
        self._trace_event(
            pending.cycle_trace,
            "pending_cycle_abandoned",
            cycle_id=pending.cycle_id,
            reason=normalized_reason,
        )
        session.last_cycle_trace = list(pending.cycle_trace)
        session.pending_cycle = None
        return pending.cycle_id
