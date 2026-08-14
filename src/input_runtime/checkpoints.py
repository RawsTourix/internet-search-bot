"""Unified IR-4 checkpoint orchestration."""
from __future__ import annotations
from collections import defaultdict
from typing import Any
from .applier import CycleInputApplier
from .errors import InputRuntimeConflictError
from .handoff import RuntimeHandoffState
from .models import ActiveCycleSnapshot, ClaimedInboxRange, CheckpointAction, CheckpointName, CheckpointOutcome, CycleStatus, InboxState

class InputRuntimeCheckpointService:
    """Run one protocol-safe checkpoint for an active cycle.

    The accepted watermark is captured at checkpoint entry. The service may
    execute several bounded FIFO apply operations to reach that watermark, but
    each claim still respects the configured count/byte limits and creates
    exactly one update plus one context revision.
    """
    def __init__(self, *, applier: CycleInputApplier) -> None:
        self.applier = applier

    async def ensure_initial_context(self, *, checkpoint: CheckpointName, active_cycle: Any, input_batch_id: str) -> CheckpointOutcome:
        return await self.applier.ensure_initial_context(session_id=str(active_cycle.session_id), cycle_id=str(active_cycle.cycle_id), generation=int(getattr(active_cycle, 'input_runtime_generation', 0)), checkpoint=checkpoint, active_cycle=active_cycle, input_batch_id=input_batch_id)

    async def _reconcile_expired_claims(self, *, cycle_id: str, generation: int, checkpoint: CheckpointName) -> CheckpointOutcome | None:
        now = self.applier._now()
        groups: dict[str, list[Any]] = defaultdict(list)
        for item in await self.applier.repositories.inbox.list_for_cycle(cycle_id):
            if item.generation == generation and item.state in {InboxState.CLAIMED, InboxState.APPLYING} and item.claim_token and item.claim_expires_at is not None and item.claim_expires_at <= now:
                groups[item.claim_token].append(item)
        for token, group in groups.items():
            group.sort(key=lambda item: item.cycle_sequence)
            for item in group:
                marker = await self.applier.repositories.handoffs.get(item.admission_id)
                if marker is not None and marker.state in {RuntimeHandoffState.HANDED_OFF, RuntimeHandoffState.AMBIGUOUS}:
                    return CheckpointOutcome(checkpoint=checkpoint, action=CheckpointAction.INTERRUPT, reason_code='expired_post_handoff_claim_requires_recovery')
            claim = ClaimedInboxRange(cycle_id=cycle_id, generation=generation, claim_token=token, first_cycle_sequence=group[0].cycle_sequence, last_cycle_sequence=group[-1].cycle_sequence, items=tuple(group), claimed_bytes=sum(item.payload_size_bytes for item in group), claim_expires_at=group[0].claim_expires_at)
            await self.applier.repositories.inbox.requeue_claim(claim, error_code='expired_checkpoint_claim_requeued')
        return None

    async def _persist_checkpoint_snapshot(self, *, checkpoint: CheckpointName, active_cycle: Any, desired_status: CycleStatus | None, waiting_question: str | None, interruption_reason: str | None) -> CheckpointOutcome:
        repositories = self.applier.repositories
        session_id, cycle_id = str(active_cycle.session_id), str(active_cycle.cycle_id)
        generation = int(getattr(active_cycle, 'input_runtime_generation', 0))
        for _ in range(8):
            state = await repositories.sessions.get(session_id)
            snapshot = await repositories.snapshots.get(cycle_id)
            if state is None or snapshot is None or state.active_cycle_id != cycle_id or state.generation != generation or snapshot.session_id != session_id or snapshot.generation != generation:
                return CheckpointOutcome(checkpoint=checkpoint, action=CheckpointAction.INTERRUPT, reason_code='checkpoint_cycle_authority_mismatch')
            if snapshot.applied_through_cycle_sequence > state.active_cycle_accepted_through_sequence:
                return CheckpointOutcome(checkpoint=checkpoint, action=CheckpointAction.INTERRUPT, context_revision_id=snapshot.active_context_revision_id, applied_through_cycle_sequence=snapshot.applied_through_cycle_sequence, reason_code='snapshot_watermark_exceeds_accepted_input')
            status = desired_status or self.applier._status(getattr(active_cycle, 'status', None))
            question = waiting_question
            if status == CycleStatus.WAITING_USER:
                question = question or getattr(active_cycle, 'waiting_question', None)
                if not question:
                    return CheckpointOutcome(checkpoint=checkpoint, action=CheckpointAction.INTERRUPT, context_revision_id=snapshot.active_context_revision_id, applied_through_cycle_sequence=snapshot.applied_through_cycle_sequence, reason_code='waiting_checkpoint_missing_question')
            else:
                question = None
            reason = interruption_reason
            if status == CycleStatus.INTERRUPTED:
                reason = reason or getattr(active_cycle, 'interruption_reason', None) or 'input_runtime_checkpoint_interrupted'
            else:
                reason = None
            candidate = snapshot.model_copy(update={'status': status, 'messages_for_llm': [dict(item) for item in active_cycle.messages_for_llm], 'cycle_trace': [dict(item) for item in active_cycle.cycle_trace], 'waiting_question': question, 'interruption_reason': reason, 'active_plan_id': getattr(active_cycle, 'active_plan_id', None), 'active_plan_revision': getattr(active_cycle, 'active_plan_revision', None), 'active_plan_node_id': getattr(active_cycle, 'active_plan_node_id', None), 'artifact_refs': list(dict.fromkeys(getattr(active_cycle, 'artifact_refs', ()) or ())), 'read_artifact_refs': list(dict.fromkeys(getattr(active_cycle, 'read_artifact_refs', ()) or ())), 'result_refs': list(dict.fromkeys(getattr(active_cycle, 'result_refs', ()) or ())), 'safe_checkpoint': checkpoint, 'snapshot_revision': snapshot.snapshot_revision + 1, 'updated_at': self.applier._now()})
            candidate = ActiveCycleSnapshot.model_validate(candidate.model_dump(mode='python'))
            ignored = {'snapshot_revision', 'updated_at'}
            if {k: v for k, v in candidate.model_dump(mode='python').items() if k not in ignored} == {k: v for k, v in snapshot.model_dump(mode='python').items() if k not in ignored}:
                self.applier._install_snapshot(active_cycle, snapshot)
                return CheckpointOutcome(checkpoint=checkpoint, action=CheckpointAction.CONTINUE, context_revision_id=snapshot.active_context_revision_id, applied_through_cycle_sequence=snapshot.applied_through_cycle_sequence)
            try:
                persisted = await repositories.snapshots.compare_and_swap(snapshot.snapshot_revision, candidate)
            except InputRuntimeConflictError:
                continue
            self.applier._install_snapshot(active_cycle, persisted)
            return CheckpointOutcome(checkpoint=checkpoint, action=CheckpointAction.CONTINUE, context_revision_id=persisted.active_context_revision_id, applied_through_cycle_sequence=persisted.applied_through_cycle_sequence)
        return CheckpointOutcome(checkpoint=checkpoint, action=CheckpointAction.INTERRUPT, reason_code='checkpoint_snapshot_cas_exhausted')

    async def _ambiguous_pending_reason(self, *, active_cycle: Any, after_sequence: int) -> str | None:
        for item in await self.applier.repositories.inbox.list_for_cycle(str(active_cycle.cycle_id)):
            if item.cycle_sequence <= after_sequence:
                continue
            marker = await self.applier.repositories.handoffs.get(item.admission_id)
            if marker is None:
                continue
            if marker.state == RuntimeHandoffState.AMBIGUOUS:
                return 'ambiguous_runtime_handoff_requires_recovery'
            if item.state == InboxState.APPLYING and marker.state == RuntimeHandoffState.HANDED_OFF:
                return 'post_handoff_applying_claim_requires_recovery'
        return None

    async def run_checkpoint(self, *, checkpoint: CheckpointName, active_cycle: Any, desired_status: CycleStatus | None=None, waiting_question: str | None=None, interruption_reason: str | None=None, apply_input: bool=True) -> CheckpointOutcome:
        input_batch_id = str(getattr(active_cycle, 'original_input_batch_id', '') or '')
        if not getattr(active_cycle, 'active_context_revision_id', None):
            if not input_batch_id:
                return CheckpointOutcome(checkpoint=checkpoint, action=CheckpointAction.INTERRUPT, reason_code='initial_input_identity_unavailable')
            initial = await self.ensure_initial_context(checkpoint=checkpoint, active_cycle=active_cycle, input_batch_id=input_batch_id)
            if initial.action == CheckpointAction.INTERRUPT:
                return initial
        repositories = self.applier.repositories
        state = await repositories.sessions.get(str(active_cycle.session_id))
        snapshot = await repositories.snapshots.get(str(active_cycle.cycle_id))
        generation = int(getattr(active_cycle, 'input_runtime_generation', 0))
        if state is None or snapshot is None or state.active_cycle_id != str(active_cycle.cycle_id) or state.generation != generation or snapshot.session_id != str(active_cycle.session_id) or snapshot.generation != state.generation:
            return CheckpointOutcome(checkpoint=checkpoint, action=CheckpointAction.INTERRUPT, reason_code='checkpoint_cycle_authority_mismatch')
        now = self.applier._now()
        await self.applier._mark_snapshot_applied_records(snapshot, now=now)
        state = await self.applier._advance_session_authority(state=state, context_revision_id=snapshot.active_context_revision_id, applied_through=snapshot.applied_through_cycle_sequence, now=now)
        self.applier._install_snapshot(active_cycle, snapshot)
        interrupted = await self._reconcile_expired_claims(cycle_id=str(active_cycle.cycle_id), generation=generation, checkpoint=checkpoint)
        if interrupted is not None:
            return interrupted
        target_accepted = state.active_cycle_accepted_through_sequence
        applied_through = int(getattr(active_cycle, 'applied_through_cycle_sequence', 0))
        revision_id = getattr(active_cycle, 'active_context_revision_id', None)
        applied_ids: list[str] = []
        while apply_input and applied_through < target_accepted:
            reason = await self._ambiguous_pending_reason(active_cycle=active_cycle, after_sequence=applied_through)
            if reason:
                return CheckpointOutcome(checkpoint=checkpoint, action=CheckpointAction.INTERRUPT, context_revision_id=revision_id, applied_through_cycle_sequence=applied_through, reason_code=reason)
            outcome = await self.applier.apply_pending_input(session_id=str(active_cycle.session_id), cycle_id=str(active_cycle.cycle_id), generation=generation, checkpoint=checkpoint, active_cycle=active_cycle)
            if outcome.action == CheckpointAction.INTERRUPT:
                return outcome
            if outcome.action != CheckpointAction.INPUT_APPLIED:
                return CheckpointOutcome(checkpoint=checkpoint, action=CheckpointAction.INTERRUPT, context_revision_id=outcome.context_revision_id, applied_through_cycle_sequence=outcome.applied_through_cycle_sequence, reason_code='accepted_input_range_unavailable')
            applied_ids.extend(outcome.applied_input_batch_ids)
            revision_id, applied_through = outcome.context_revision_id, outcome.applied_through_cycle_sequence
        if applied_ids:
            return CheckpointOutcome(checkpoint=checkpoint, action=CheckpointAction.INPUT_APPLIED, context_revision_id=revision_id, applied_through_cycle_sequence=applied_through, applied_input_batch_ids=tuple(applied_ids))
        return await self._persist_checkpoint_snapshot(checkpoint=checkpoint, active_cycle=active_cycle, desired_status=desired_status, waiting_question=waiting_question, interruption_reason=interruption_reason)
