"""Targeted IR-5 repair layers over the reusable control/checkpoint core."""
from __future__ import annotations

from typing import Any

from .ir5_checkpoints import ControlAwareCheckpointService
from .ir5_controls import InputRuntimeControlService
from .models import CheckpointName, ControlState, CycleStatus, SessionControlCommand


class HardenedInputRuntimeControlService(InputRuntimeControlService):
    """Repair record-first publication and pass canonical cancellation time."""

    async def _existing(self, **kwargs: Any) -> SessionControlCommand | None:
        existing = await super()._existing(**kwargs)
        if existing is None:
            return None
        # A crash may have persisted the immutable control record but failed the
        # session pending-watermark write. Re-enter the repository allocation
        # protocol before any semantic retry; it reuses the same control ID and
        # sequence and repairs only missing acceptance metadata.
        return await self.repositories.controls.allocate(existing)  # type: ignore[attr-defined]

    async def _reconcile_reset(self, command: SessionControlCommand):
        controls = self.repositories.controls
        current, state = await controls.accept_reset(command)  # type: ignore[attr-defined]
        old_generation = command.generation
        reason = "session_reset"
        cancelled_at = self._now()
        await self.repositories.admissions.cancel_generation(
            command.session_id,
            generation=old_generation,
            cancelled_at=cancelled_at,
            reason_code=reason,
        )
        await self.repositories.inbox.cancel_generation(
            command.session_id,
            generation=old_generation,
            cancelled_at=cancelled_at,
            reason_code=reason,
        )
        await controls.cancel_generation_except(  # type: ignore[attr-defined]
            command.session_id,
            generation=old_generation,
            reason_code=reason,
            exclude_control_ids=(command.control_id,),
        )
        await self.repositories.snapshots.cancel_generation(
            command.session_id,
            generation=old_generation,
            reason_code=reason,
        )
        await self.repositories.emissions.cancel_generation(
            command.session_id,
            generation=old_generation,
            reason_code=reason,
        )
        await self.repositories.finalizations.cancel_generation(
            command.session_id,
            generation=old_generation,
            reason_code=reason,
        )
        if current.state != ControlState.APPLIED:
            current = await self.repositories.controls.apply(
                command.control_id,
                applied_at=self._now(),
            )
        await self._advance_applied_watermark(command.session_id)
        coordinator = self.wake_coordinator
        if coordinator is not None:
            synchronize = getattr(coordinator, "synchronize_generation", None)
            if callable(synchronize):
                await synchronize(command.session_id, generation=state.generation)
        return self._control_outcome(
            current=current,
            effective_cycle_status=CycleStatus.IDLE,
        )

    @staticmethod
    def _control_outcome(*, current: SessionControlCommand, effective_cycle_status):
        from .models import ControlOutcome

        return ControlOutcome(
            outcome=current.state,
            command=current,
            effective_cycle_status=effective_cycle_status,
        )


class HardenedControlAwareCheckpointService(ControlAwareCheckpointService):
    """Do not let CP-RESUME overwrite a durable pause before reduction."""

    async def _persist_closed_context(
        self,
        *,
        checkpoint: CheckpointName,
        active_cycle: Any,
    ):
        snapshot = await self.applier.repositories.snapshots.get(
            str(active_cycle.cycle_id)
        )
        if snapshot is not None and snapshot.status == CycleStatus.PAUSED_BY_USER:
            # A genuinely paused cycle has not executed a new atomic block yet.
            # Its durable context is already the closed pause snapshot.  Calling
            # the IR-4 closed-context sync here would derive status from stale
            # in-memory RUNNING and would erase waiting-question metadata before
            # the continue reducer gets to inspect it.
            return None
        return await super()._persist_closed_context(
            checkpoint=checkpoint,
            active_cycle=active_cycle,
        )
