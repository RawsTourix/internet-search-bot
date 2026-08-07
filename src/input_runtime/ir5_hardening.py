"""Targeted IR-5 repair layers over the reusable control/checkpoint core."""
from __future__ import annotations

from typing import Any

from .ir5_checkpoints import ControlAwareCheckpointService
from .ir5_controls import InputRuntimeControlService
from .models import (
    CheckpointAction,
    CheckpointName,
    ControlCommandType,
    ControlOutcome,
    ControlState,
    CycleStatus,
    SessionControlCommand,
)


_TERMINAL_CONTROL_STATES = {
    ControlState.APPLIED,
    ControlState.REJECTED,
    ControlState.CANCELLED,
}
_TERMINAL_OR_IDLE = {
    CycleStatus.IDLE,
    CycleStatus.DONE,
    CycleStatus.ERROR,
    CycleStatus.CANCELLED,
}


class HardenedInputRuntimeControlService(InputRuntimeControlService):
    """Repair publication gaps and classify controls from durable order."""

    async def _existing(self, **kwargs: Any) -> SessionControlCommand | None:
        existing = await super()._existing(**kwargs)
        if existing is None:
            return None
        return await self.repositories.controls.allocate(existing)  # type: ignore[attr-defined]

    async def _has_effective_pending_pause(self, state: Any) -> bool:
        if state.pending_control_sequence <= state.applied_control_sequence:
            return False
        rows = await self.repositories.controls.list_range(  # type: ignore[attr-defined]
            state.session_id,
            after_sequence=state.applied_control_sequence,
            through_sequence=state.pending_control_sequence,
        )
        pending_pause = False
        for row in rows:
            if row.state in _TERMINAL_CONTROL_STATES:
                continue
            if row.generation != state.generation:
                continue
            if (
                state.active_cycle_id is not None
                and row.target_cycle_id != state.active_cycle_id
            ):
                continue
            if row.command == ControlCommandType.RESET:
                return False
            if row.command == ControlCommandType.PAUSE:
                pending_pause = True
            elif row.command == ControlCommandType.CONTINUE:
                pending_pause = False
        return pending_pause

    async def request_continue(
        self,
        *,
        session_id: str,
        idempotency_key: str,
        source_client_type: str,
        source_message_ref: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> ControlOutcome:
        existing = await self._existing(
            session_id=session_id,
            idempotency_key=idempotency_key,
            command=ControlCommandType.CONTINUE,
            source_client_type=source_client_type,
            source_message_ref=source_message_ref,
        )
        if existing is not None:
            if existing.state == ControlState.QUEUED:
                return await self._repair_continue(existing)
            state = await self._state(session_id)
            return ControlOutcome(
                outcome=existing.state,
                command=existing,
                effective_cycle_status=state.cycle_status,
            )

        state = await self._state(session_id)
        pending_pause = await self._has_effective_pending_pause(state)
        rejection = None
        if state.cycle_status in _TERMINAL_OR_IDLE or state.active_cycle_id is None:
            rejection = "nothing_to_continue"
        elif (
            state.cycle_status in {CycleStatus.RUNNING, CycleStatus.FINALIZING}
            and not pending_pause
        ):
            rejection = "already_running"
        elif state.cycle_status == CycleStatus.WAITING_USER:
            rejection = "still_waiting_for_input"

        target = state.active_cycle_id or self._inactive_target(session_id)
        command = SessionControlCommand(
            control_id=self.control_id_factory(),
            session_id=session_id,
            target_cycle_id=target,
            generation=state.generation,
            sequence_number=1,
            command=ControlCommandType.CONTINUE,
            state=ControlState.REJECTED if rejection else ControlState.QUEUED,
            idempotency_key=idempotency_key,
            source_client_type=source_client_type,
            source_message_ref=self._source_ref(
                source_message_ref,
                accepted_input_through_sequence=(
                    state.active_cycle_accepted_through_sequence
                    if rejection is None
                    else None
                ),
            ),
            reason=reason,
            created_at=self._now(),
            rejection_code=rejection,
        )
        allocated = await self.repositories.controls.allocate(command)  # type: ignore[attr-defined]
        if allocated.state == ControlState.REJECTED:
            await self._advance_applied_watermark(session_id)
            return ControlOutcome(
                outcome=allocated.state,
                command=allocated,
                effective_cycle_status=state.cycle_status,
            )
        return await self._repair_continue(allocated)

    async def reduce_at_checkpoint(self, **kwargs: Any):
        outcome = await super().reduce_at_checkpoint(**kwargs)
        if (
            outcome is None
            or outcome.action != CheckpointAction.WAIT
            or kwargs.get("checkpoint") != CheckpointName.RESUME
        ):
            return outcome

        active_cycle = kwargs["active_cycle"]
        cycle_id = str(active_cycle.cycle_id)
        generation = int(getattr(active_cycle, "input_runtime_generation", 0))
        through = int(kwargs["through_control_sequence"])
        snapshot = await self.repositories.snapshots.get(cycle_id)
        if snapshot is None or snapshot.generation != generation:
            return outcome

        rows = await self.repositories.controls.list_range(  # type: ignore[attr-defined]
            str(active_cycle.session_id),
            after_sequence=0,
            through_sequence=through,
        )
        resume_target: int | None = None
        for row in rows:
            if (
                row.command == ControlCommandType.CONTINUE
                and row.generation == generation
                and row.target_cycle_id == cycle_id
            ):
                candidate = self.resume_input_target(row)
                if candidate is not None:
                    resume_target = candidate
        if (
            resume_target is not None
            and resume_target > snapshot.applied_through_cycle_sequence
        ):
            return None
        return outcome

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
        return ControlOutcome(
            outcome=current.state,
            command=current,
            effective_cycle_status=effective_cycle_status,
        )


class HardenedControlAwareCheckpointService(ControlAwareCheckpointService):
    """Preserve pause authority and resolve WAITING only after real input."""

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
            return None
        return await super()._persist_closed_context(
            checkpoint=checkpoint,
            active_cycle=active_cycle,
        )

    async def run_checkpoint(self, **kwargs: Any):
        outcome = await super().run_checkpoint(**kwargs)
        if (
            kwargs.get("checkpoint") != CheckpointName.RESUME
            or outcome.action != CheckpointAction.INPUT_APPLIED
        ):
            return outcome

        active_cycle = kwargs["active_cycle"]
        session_id = str(active_cycle.session_id)
        cycle_id = str(active_cycle.cycle_id)
        generation = int(getattr(active_cycle, "input_runtime_generation", 0))
        snapshot = await self.applier.repositories.snapshots.get(cycle_id)
        state = await self.applier.repositories.sessions.get(session_id)
        if (
            snapshot is None
            or state is None
            or snapshot.generation != generation
            or state.generation != generation
            or state.active_cycle_id != cycle_id
        ):
            return outcome
        if (
            snapshot.status != CycleStatus.WAITING_USER
            and state.cycle_status != CycleStatus.WAITING_USER
            and snapshot.waiting_question is None
        ):
            return outcome

        # A real applied input, not /continue itself, resolves the active WAITING
        # boundary. This happens only after the base service drains the captured
        # continue target completely, including multiple bounded ranges.
        persisted = await self.control_service._persist_snapshot_status(
            session_id=session_id,
            cycle_id=cycle_id,
            generation=generation,
            status=CycleStatus.RUNNING,
            checkpoint=CheckpointName.RESUME,
        )
        self.applier._install_snapshot(active_cycle, persisted)
        return outcome
