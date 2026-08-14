"""IR-5 control-aware safe checkpoint protocol."""
from __future__ import annotations

from typing import Any

from .errors import InputRuntimeConflictError
from .ir4_checkpoint_contracts import EntryWatermarkCheckpointService
from .ir5_controls import InputRuntimeControlService
from .models import (
    CheckpointAction,
    CheckpointName,
    CheckpointOutcome,
    ControlCommandType,
    CycleStatus,
)


class ControlAwareCheckpointService(EntryWatermarkCheckpointService):
    """Apply controls captured at checkpoint entry before ordinary input.

    Control and input use independent entry watermarks.  A continue command also
    carries the input watermark that existed at its own acceptance boundary; at
    CP-RESUME that target is drained completely (in bounded chunks) before the
    first resumed LLM request.  Input accepted after continue remains for the
    next ordinary running checkpoint.
    """

    def __init__(self, *, applier: Any, control_service: InputRuntimeControlService):
        super().__init__(applier=applier)
        self.control_service = control_service

    async def _resume_target(
        self,
        *,
        session_id: str,
        after_sequence: int,
        through_sequence: int,
    ) -> int | None:
        rows = await self.applier.repositories.controls.list_range(
            session_id,
            after_sequence=after_sequence,
            through_sequence=through_sequence,
        )
        target: int | None = None
        for row in rows:
            if row.command != ControlCommandType.CONTINUE:
                continue
            candidate = self.control_service.resume_input_target(row)
            if candidate is not None:
                target = candidate
        return target

    async def _run_checkpoint_impl(
        self,
        *,
        checkpoint: CheckpointName,
        active_cycle: Any,
        desired_status: CycleStatus | None = None,
        waiting_question: str | None = None,
        interruption_reason: str | None = None,
        apply_input: bool = True,
        terminal_sync: bool = False,
    ) -> CheckpointOutcome:
        repositories = self.applier.repositories
        session_id = str(active_cycle.session_id)
        cycle_id = str(active_cycle.cycle_id)
        generation = int(getattr(active_cycle, "input_runtime_generation", 0))

        entry_state = await repositories.sessions.get(session_id)
        if (
            entry_state is None
            or entry_state.active_cycle_id != cycle_id
            or entry_state.generation != generation
        ):
            return CheckpointOutcome(
                checkpoint=checkpoint,
                action=CheckpointAction.INTERRUPT,
                reason_code="checkpoint_cycle_authority_mismatch",
            )
        entry_accepted = entry_state.active_cycle_accepted_through_sequence
        entry_control = entry_state.pending_control_sequence
        entry_applied_control = entry_state.applied_control_sequence
        resume_target = (
            await self._resume_target(
                session_id=session_id,
                after_sequence=entry_applied_control,
                through_sequence=entry_control,
            )
            if checkpoint == CheckpointName.RESUME
            else None
        )

        # The current LLM/tool/final-processing block is complete at checkpoint
        # entry. Persist that protocol-valid closed context before interpreting
        # controls which may pause or fence the cycle.
        closed = await self._persist_closed_context(
            checkpoint=checkpoint,
            active_cycle=active_cycle,
        )
        if closed is not None and closed.action == CheckpointAction.INTERRUPT:
            return closed

        input_batch_id = str(
            getattr(active_cycle, "original_input_batch_id", "") or ""
        )
        if not getattr(active_cycle, "active_context_revision_id", None):
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

        state = await repositories.sessions.get(session_id)
        snapshot = await repositories.snapshots.get(cycle_id)
        if (
            state is None
            or snapshot is None
            or state.active_cycle_id != cycle_id
            or state.generation != generation
            or snapshot.session_id != session_id
            or snapshot.generation != generation
        ):
            return CheckpointOutcome(
                checkpoint=checkpoint,
                action=CheckpointAction.INTERRUPT,
                reason_code="checkpoint_cycle_authority_mismatch",
            )

        control_outcome = await self.control_service.reduce_at_checkpoint(
            checkpoint=checkpoint,
            active_cycle=active_cycle,
            through_control_sequence=entry_control,
        )
        if control_outcome is not None:
            return control_outcome

        # Reducer may have resumed a real pause. Install that durable snapshot
        # before input application so in-memory state follows durable authority.
        state = await repositories.sessions.get(session_id)
        snapshot = await repositories.snapshots.get(cycle_id)
        if (
            state is None
            or snapshot is None
            or state.active_cycle_id != cycle_id
            or state.generation != generation
            or snapshot.generation != generation
        ):
            return CheckpointOutcome(
                checkpoint=checkpoint,
                action=CheckpointAction.INTERRUPT,
                reason_code="checkpoint_cycle_authority_mismatch",
            )

        target_accepted = entry_accepted
        if resume_target is not None:
            # Never let command metadata move beyond a watermark actually
            # admitted by this checkpoint entry.
            target_accepted = min(entry_accepted, resume_target)
        if snapshot.applied_through_cycle_sequence > target_accepted:
            # A duplicate CP-RESUME after the target was already consumed is a
            # no-op, not a rewind. Ordinary checkpoint semantics still forbid
            # a durable snapshot ahead of its entry watermark.
            if resume_target is not None:
                target_accepted = snapshot.applied_through_cycle_sequence
            else:
                return CheckpointOutcome(
                    checkpoint=checkpoint,
                    action=CheckpointAction.INTERRUPT,
                    context_revision_id=snapshot.active_context_revision_id,
                    applied_through_cycle_sequence=(
                        snapshot.applied_through_cycle_sequence
                    ),
                    reason_code="snapshot_watermark_exceeds_checkpoint_entry",
                )

        now = self.applier._now()
        await self.applier._mark_snapshot_applied_records(snapshot, now=now)
        state = await self.applier._advance_session_authority(
            state=state,
            context_revision_id=snapshot.active_context_revision_id,
            applied_through=snapshot.applied_through_cycle_sequence,
            now=now,
        )
        self.applier._install_snapshot(active_cycle, snapshot)

        interrupted = await self._reconcile_expired_claims(
            cycle_id=cycle_id,
            generation=generation,
            checkpoint=checkpoint,
        )
        if interrupted is not None:
            return interrupted

        applied_through = snapshot.applied_through_cycle_sequence
        revision_id = snapshot.active_context_revision_id
        applied_ids: list[str] = []
        while apply_input and applied_through < target_accepted:
            reason = await self._bounded_ambiguous_reason(
                active_cycle=active_cycle,
                after_sequence=applied_through,
                through_sequence=target_accepted,
            )
            if reason:
                return CheckpointOutcome(
                    checkpoint=checkpoint,
                    action=CheckpointAction.INTERRUPT,
                    context_revision_id=revision_id,
                    applied_through_cycle_sequence=applied_through,
                    reason_code=reason,
                )
            outcome = await self.applier.apply_pending_input(
                session_id=session_id,
                cycle_id=cycle_id,
                generation=generation,
                checkpoint=checkpoint,
                active_cycle=active_cycle,
                through_sequence=target_accepted,
            )
            if outcome.action == CheckpointAction.INTERRUPT:
                return outcome
            if outcome.action != CheckpointAction.INPUT_APPLIED:
                return CheckpointOutcome(
                    checkpoint=checkpoint,
                    action=CheckpointAction.INTERRUPT,
                    context_revision_id=outcome.context_revision_id,
                    applied_through_cycle_sequence=(
                        outcome.applied_through_cycle_sequence
                    ),
                    reason_code="accepted_input_range_unavailable",
                )
            applied_ids.extend(outcome.applied_input_batch_ids)
            revision_id = outcome.context_revision_id
            applied_through = outcome.applied_through_cycle_sequence

        if applied_ids:
            return CheckpointOutcome(
                checkpoint=checkpoint,
                action=CheckpointAction.INPUT_APPLIED,
                context_revision_id=revision_id,
                applied_through_cycle_sequence=applied_through,
                applied_input_batch_ids=tuple(applied_ids),
            )

        effective_status = desired_status
        if checkpoint == CheckpointName.BEFORE_TERMINAL_COMMIT and not terminal_sync:
            effective_status = CycleStatus.RUNNING
        return await self._persist_checkpoint_snapshot(
            checkpoint=checkpoint,
            active_cycle=active_cycle,
            desired_status=effective_status,
            waiting_question=waiting_question,
            interruption_reason=interruption_reason,
        )

    async def run_checkpoint(self, **kwargs: Any) -> CheckpointOutcome:
        try:
            return await self._run_checkpoint_impl(**kwargs)
        except InputRuntimeConflictError as error:
            snapshot = await self.applier.repositories.snapshots.get(
                str(kwargs["active_cycle"].cycle_id)
            )
            return CheckpointOutcome(
                checkpoint=kwargs["checkpoint"],
                action=CheckpointAction.INTERRUPT,
                context_revision_id=(
                    snapshot.active_context_revision_id
                    if snapshot is not None
                    else None
                ),
                applied_through_cycle_sequence=(
                    snapshot.applied_through_cycle_sequence
                    if snapshot is not None
                    else 0
                ),
                reason_code=str(error) or "control_checkpoint_conflict",
            )
