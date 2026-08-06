"""Unified IR-4 checkpoint orchestration."""

from __future__ import annotations

from typing import Any

from .applier import CycleInputApplier
from .models import CheckpointAction, CheckpointName, CheckpointOutcome


class InputRuntimeCheckpointService:
    """Run protocol-safe input checkpoints for one active cycle."""

    def __init__(self, *, applier: CycleInputApplier) -> None:
        self.applier = applier

    async def ensure_initial_context(
        self,
        *,
        checkpoint: CheckpointName,
        active_cycle: Any,
        input_batch_id: str,
    ) -> CheckpointOutcome:
        return await self.applier.ensure_initial_context(
            session_id=str(active_cycle.session_id),
            cycle_id=str(active_cycle.cycle_id),
            generation=int(getattr(active_cycle, "input_runtime_generation", 0)),
            checkpoint=checkpoint,
            active_cycle=active_cycle,
            input_batch_id=input_batch_id,
        )

    async def run_checkpoint(
        self,
        *,
        checkpoint: CheckpointName,
        active_cycle: Any,
    ) -> CheckpointOutcome:
        input_batch_id = str(
            getattr(active_cycle, "original_input_batch_id", "") or ""
        )
        active_revision = getattr(
            active_cycle, "active_context_revision_id", None
        )
        if not active_revision:
            if not input_batch_id:
                return CheckpointOutcome(
                    checkpoint=checkpoint,
                    action=CheckpointAction.INTERRUPT,
                    reason_code="initial_input_identity_unavailable",
                )
            initial = await self.ensure_initial_context(
                checkpoint=checkpoint,
                active_cycle=active_cycle,
                input_batch_id=input_batch_id,
            )
            if initial.action == CheckpointAction.INTERRUPT:
                return initial

        return await self.applier.apply_pending_input(
            session_id=str(active_cycle.session_id),
            cycle_id=str(active_cycle.cycle_id),
            generation=int(getattr(active_cycle, "input_runtime_generation", 0)),
            checkpoint=checkpoint,
            active_cycle=active_cycle,
        )
