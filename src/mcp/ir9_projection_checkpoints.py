"""IR-9 transient client projection hook for durable checkpoint outcomes.

The hook runs strictly after the existing checkpoint implementation has durably
applied an input range. It emits a ProgressEvent-shaped presentation callback;
it does not create AgentEmission records, append assistant/user history, or
participate in admission/control/finalization authority.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any

from ..input_runtime import CheckpointAction
from . import input_runtime_checkpoints as checkpoint_module


logger = logging.getLogger("MCPClient.IR9Projection")
_INSTALL_MARKER = "_ir9_projection_checkpoint_hook_installed"
_projection_progress_state: ContextVar[Any | None] = ContextVar(
    "ir9_projection_progress_state",
    default=None,
)
_projection_progress_callback: ContextVar[Any | None] = ContextVar(
    "ir9_projection_progress_callback",
    default=None,
)


async def _emit_applied_projection(
    owner: Any,
    *,
    active_cycle: Any | None,
    outcome: Any,
) -> None:
    if (
        active_cycle is None
        or outcome is None
        or outcome.action != CheckpointAction.INPUT_APPLIED
        or not outcome.applied_input_batch_ids
    ):
        return
    progress_callback = _projection_progress_callback.get()
    progress_state = _projection_progress_state.get()
    emit = getattr(owner, "_emit_progress_event", None)
    if progress_callback is None or progress_state is None or not callable(emit):
        return

    generation = int(getattr(active_cycle, "input_runtime_generation", 0) or 0)
    sequences = tuple(int(value) for value in outcome.applied_cycle_sequences)
    batch_ids = tuple(str(value) for value in outcome.applied_input_batch_ids)
    for index, input_batch_id in enumerate(batch_ids):
        cycle_sequence = sequences[index] if index < len(sequences) else 0
        await emit(
            state=progress_state,
            session_id=str(active_cycle.session_id),
            cycle_id=str(active_cycle.cycle_id),
            progress_callback=progress_callback,
            cycle_trace=getattr(active_cycle, "cycle_trace", None),
            event_type="input_addendum_applied",
            # IR-9-aware clients localize the structured event type/data. The
            # marker is content-free and safe for an older progress consumer.
            message="input_addendum_applied",
            visibility="user",
            data={
                "input_batch_id": input_batch_id,
                "cycle_sequence": cycle_sequence,
                "generation": generation,
                "locale": str(
                    getattr(progress_state, "progress_locale", "ru") or "ru"
                ),
            },
        )


def install_ir9_projection_checkpoint_hook() -> None:
    mixin = checkpoint_module.InputRuntimeCheckpointMixin
    if getattr(mixin, _INSTALL_MARKER, False):
        return

    original_activate = mixin._activate_manager_context
    original_checkpoint = mixin._run_input_checkpoint

    def activate_manager_context(
        self,
        *,
        active_cycle,
        state,
        session_id: str,
        progress_callback,
    ):
        _projection_progress_state.set(state)
        _projection_progress_callback.set(progress_callback)
        return original_activate(
            self,
            active_cycle=active_cycle,
            state=state,
            session_id=session_id,
            progress_callback=progress_callback,
        )

    async def run_input_checkpoint(self, *args, **kwargs):
        explicit_cycle = kwargs.get("active_cycle")
        outcome = await original_checkpoint(self, *args, **kwargs)
        active_cycle = (
            explicit_cycle or checkpoint_module._checkpoint_active_cycle.get()
        )
        try:
            await _emit_applied_projection(
                self,
                active_cycle=active_cycle,
                outcome=outcome,
            )
        except Exception as error:
            # Client projection failure must never change durable runtime state.
            logger.warning(
                "IR-9 applied-input projection failed: error_type=%s",
                type(error).__name__,
            )
        return outcome

    mixin._activate_manager_context = activate_manager_context
    mixin._run_input_checkpoint = run_input_checkpoint
    setattr(mixin, _INSTALL_MARKER, True)
