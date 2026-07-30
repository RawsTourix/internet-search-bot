"""Prevent implicit artifact authority from leaking into a later AgentCycle."""

from __future__ import annotations

from typing import Any

from .artifact_request_context import get_artifact_request_input_batch
from .manager_runtime_context import get_manager_context


class ArtifactHistoryIsolationMixin:
    """Keep current-cycle refs while removing legacy cross-cycle handoffs.

    Historical exact versions remain discoverable through
    ``artifact_list(scope='session')``.  They are deliberately not injected into
    a fresh cycle before that authoritative catalog operation activates them.
    """

    def _append_dialog_turn(self, session, **kwargs: Any) -> None:
        super()._append_dialog_turn(session, **kwargs)
        context = get_manager_context()
        if context is None:
            return
        handoffs = getattr(self, "_session_artifact_handoffs", None)
        if isinstance(handoffs, dict):
            handoffs.pop(context.session_id, None)

    def _activate_manager_context(
        self,
        *,
        active_cycle,
        state,
        session_id,
        progress_callback,
    ):
        # Repeated activations inside one cycle must preserve artifacts created,
        # modified, read or explicitly activated earlier in that same cycle.
        retained_refs = list(active_cycle.artifact_refs)
        input_batch = get_artifact_request_input_batch()
        if input_batch is not None:
            retained_refs.extend(input_batch.artifact_refs)
            retained_refs.extend(input_batch.referenced_artifact_refs)

        context = super()._activate_manager_context(
            active_cycle=active_cycle,
            state=state,
            session_id=session_id,
            progress_callback=progress_callback,
        )
        allowed = list(dict.fromkeys(retained_refs))
        removed = [
            artifact_id
            for artifact_id in context.active_cycle.artifact_refs
            if artifact_id not in allowed
        ]
        context.active_cycle.artifact_refs = allowed
        if removed:
            self._trace_event(
                context.active_cycle.cycle_trace,
                "artifact_implicit_history_removed",
                artifact_count=len(removed),
                artifact_ids=removed,
            )
        return context
