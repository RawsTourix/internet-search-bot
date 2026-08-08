"""IR-4 entry-watermark, cancellation, and managed-error hardening."""
from __future__ import annotations
import asyncio
from dataclasses import replace
from typing import Any, Callable
from ..memory import CycleSegmentSelectionError, validate_openai_tool_sequence
from .applier import CycleInputApplier
from .checkpoint_hardening import DurableContextCheckpointService
from .errors import InputRuntimeConflictError
from .factory import InputRuntimeRepositories
from .handoff import RuntimeHandoffState
from .models import ActiveCycleSnapshot, AdmissionState, CheckpointAction, CheckpointName, CheckpointOutcome, ClaimedInboxRange, CycleStatus, InboxState

class EntryBoundInboxRepository:
    """Constrain one repository claim to the checkpoint entry watermark."""

    def __init__(self, delegate: Any, *, through_sequence: int, claim_observer: Callable[[ClaimedInboxRange | None], None]) -> None:
        self._delegate = delegate
        self._through_sequence = through_sequence
        self._claim_observer = claim_observer

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    async def claim_contiguous_range(self, cycle_id: str, *, generation: int, after_sequence: int, max_items: int, max_bytes: int, lease_seconds: int, through_sequence: int | None=None) -> ClaimedInboxRange | None:
        upper_bound = min(self._through_sequence, self._through_sequence if through_sequence is None else through_sequence)
        if upper_bound <= after_sequence:
            return None
        records = [item for item in await self._delegate.list_for_cycle(cycle_id) if item.generation == generation and after_sequence < item.cycle_sequence <= upper_bound and (item.state == InboxState.QUEUED)]
        records.sort(key=lambda item: item.cycle_sequence)
        if not records or records[0].cycle_sequence != after_sequence + 1:
            return None
        selected_count = 0
        selected_bytes = 0
        expected = after_sequence + 1
        for item in records:
            if item.cycle_sequence != expected or selected_count >= max_items:
                break
            size = item.payload_size_bytes
            if size > max_bytes and selected_count == 0:
                return None
            if selected_count and selected_bytes + size > max_bytes:
                break
            selected_count += 1
            selected_bytes += size
            expected += 1
        if selected_count == 0:
            return None
        claim = await self._delegate.claim_contiguous_range(cycle_id, generation=generation, after_sequence=after_sequence, max_items=selected_count, max_bytes=max_bytes, lease_seconds=lease_seconds)
        self._claim_observer(claim)
        if claim is not None and claim.last_cycle_sequence > upper_bound:
            raise InputRuntimeConflictError('repository claim exceeded checkpoint entry watermark')
        return claim

    async def mark_applying(self, claim: ClaimedInboxRange) -> ClaimedInboxRange:
        self._claim_observer(claim)
        applying = await self._delegate.mark_applying(claim)
        self._claim_observer(applying)
        return applying

class PreclaimedInboxRepository:
    """Present one already bounded claim to the canonical apply protocol."""

    def __init__(self, delegate: Any, *, claim: ClaimedInboxRange | None, claim_observer: Callable[[ClaimedInboxRange | None], None]) -> None:
        self._delegate = delegate
        self._claim = claim
        self._returned = False
        self._claim_observer = claim_observer

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)

    async def claim_contiguous_range(self, *args: Any, **kwargs: Any):
        if self._returned:
            return None
        self._returned = True
        self._claim_observer(self._claim)
        return self._claim

    async def mark_applying(self, claim: ClaimedInboxRange) -> ClaimedInboxRange:
        self._claim_observer(claim)
        applying = await self._delegate.mark_applying(claim)
        self._claim_observer(applying)
        return applying

class ManagedCommittedBatchReader:
    """Normalize missing immutable payloads into a managed conflict."""

    def __init__(self, delegate: Any) -> None:
        self._delegate = delegate

    async def get_committed(self, input_batch_id: str):
        try:
            return await self._delegate.get_committed(input_batch_id)
        except (KeyError, LookupError) as error:
            raise InputRuntimeConflictError('committed batch unavailable') from error

class CancellationSafeCycleInputApplier(CycleInputApplier):
    """Apply one bounded range with cancellation-safe durable cleanup."""

    @staticmethod
    def _managed_reason(error: Exception) -> str:
        if isinstance(error, CycleSegmentSelectionError):
            return 'invalid_protocol_sequence'
        message = str(error).lower()
        if 'repository claim exceeded' in message:
            return 'claim_exceeded_entry_watermark'
        if 'committed batch identity' in message or 'committed batch unavailable' in message or 'inbox/admission' in message:
            return 'invalid_committed_relation'
        if 'claimed input range has a gap' in message:
            return 'invalid_claimed_input_range'
        if 'ambiguous runtime handoff' in message:
            return 'ambiguous_runtime_handoff_requires_recovery'
        if 'divergent' in message or 'context revision' in message:
            return 'divergent_context_revision'
        if 'stale snapshot' in message or 'snapshot revision' in message:
            return 'checkpoint_snapshot_cas_exhausted'
        return 'checkpoint_apply_conflict'

    @staticmethod
    def _claim_from_current(claim: ClaimedInboxRange, current_items: list[Any]) -> ClaimedInboxRange:
        current_items.sort(key=lambda item: item.cycle_sequence)
        return claim.model_copy(update={'first_cycle_sequence': current_items[0].cycle_sequence, 'last_cycle_sequence': current_items[-1].cycle_sequence, 'items': tuple(current_items), 'claimed_bytes': sum((item.payload_size_bytes for item in current_items)), 'claim_expires_at': current_items[0].claim_expires_at})

    async def _reconcile_claim_after_abort(self, *, claim: ClaimedInboxRange, active_cycle: Any, error_code: str) -> None:
        snapshot = await self.repositories.snapshots.get(claim.cycle_id)
        applied_by_snapshot = snapshot is not None and snapshot.generation == claim.generation and (snapshot.applied_through_cycle_sequence >= claim.last_cycle_sequence) and all((item.input_batch_id in snapshot.applied_input_batch_ids for item in claim.items))
        if applied_by_snapshot:
            assert snapshot is not None
            self._install_snapshot(active_cycle, snapshot)
            now = self._now()
            await self._mark_snapshot_applied_records(snapshot, now=now)
            state = await self.repositories.sessions.get(snapshot.session_id)
            if state is not None and state.active_cycle_id == snapshot.cycle_id and (state.generation == snapshot.generation):
                await self._advance_session_authority(state=state, context_revision_id=snapshot.active_context_revision_id, applied_through=snapshot.applied_through_cycle_sequence, now=now)
            return
        rows = {item.inbox_item_id: item for item in await self.repositories.inbox.list_for_cycle(claim.cycle_id)}
        current = [rows.get(item.inbox_item_id) for item in claim.items]
        if any((item is None for item in current)):
            raise InputRuntimeConflictError('claim cleanup lost inbox identity')
        current_items = [item for item in current if item is not None]
        if all((item.state == InboxState.QUEUED for item in current_items)):
            return
        if all((item.state == InboxState.APPLIED for item in current_items)):
            return
        if all((item.state in {InboxState.CLAIMED, InboxState.APPLYING} and item.claim_token == claim.claim_token and (item.generation == claim.generation) for item in current_items)):
            await self.repositories.inbox.requeue_claim(self._claim_from_current(claim, current_items), error_code=error_code)
            return
        raise InputRuntimeConflictError('claim cleanup state diverged')

    @staticmethod
    async def _await_cleanup(awaitable: Any) -> None:
        cleanup_task = asyncio.create_task(awaitable)
        while True:
            try:
                await asyncio.shield(cleanup_task)
                return
            except asyncio.CancelledError:
                if cleanup_task.done():
                    cleanup_task.result()
                    return
                continue

    async def apply_pending_input(self, *, session_id: str, cycle_id: str, generation: int, checkpoint: CheckpointName, active_cycle: Any, through_sequence: int | None=None) -> CheckpointOutcome:
        if through_sequence is None:
            state = await self.repositories.sessions.get(session_id)
            if state is None or state.active_cycle_id != cycle_id or state.generation != generation:
                return CheckpointOutcome(checkpoint=checkpoint, action=CheckpointAction.INTERRUPT, reason_code='checkpoint_cycle_authority_mismatch')
            through_sequence = state.active_cycle_accepted_through_sequence
        if through_sequence < 0:
            return CheckpointOutcome(checkpoint=checkpoint, action=CheckpointAction.INTERRUPT, reason_code='invalid_checkpoint_entry_watermark')
        active_claim: ClaimedInboxRange | None = None

        def remember(claim: ClaimedInboxRange | None) -> None:
            nonlocal active_claim
            if claim is not None:
                active_claim = claim
        snapshot = await self.repositories.snapshots.get(cycle_id)
        if snapshot is None or snapshot.session_id != session_id or snapshot.generation != generation:
            return CheckpointOutcome(checkpoint=checkpoint, action=CheckpointAction.INTERRUPT, reason_code='checkpoint_cycle_authority_mismatch')
        bounded_inbox = EntryBoundInboxRepository(self.repositories.inbox, through_sequence=through_sequence, claim_observer=remember)
        preclaimed = await bounded_inbox.claim_contiguous_range(cycle_id, generation=generation, after_sequence=snapshot.applied_through_cycle_sequence, through_sequence=through_sequence, max_items=self.config.max_batches_per_checkpoint, max_bytes=self.config.max_batch_bytes_per_checkpoint, lease_seconds=self.config.claim_lease_seconds)
        preclaimed_inbox = PreclaimedInboxRepository(self.repositories.inbox, claim=preclaimed, claim_observer=remember)
        bounded_repositories: InputRuntimeRepositories = replace(self.repositories, inbox=preclaimed_inbox)
        delegate = CycleInputApplier(config=self.config, repositories=bounded_repositories, committed_batches=ManagedCommittedBatchReader(self.committed_batches), clock=self.clock, revision_id_factory=self.revision_id_factory)
        try:
            return await delegate.apply_pending_input(session_id=session_id, cycle_id=cycle_id, generation=generation, checkpoint=checkpoint, active_cycle=active_cycle)
        except asyncio.CancelledError:
            if active_claim is not None:
                await self._await_cleanup(self._reconcile_claim_after_abort(claim=active_claim, active_cycle=active_cycle, error_code='checkpoint_apply_cancelled'))
            raise
        except (InputRuntimeConflictError, CycleSegmentSelectionError) as error:
            if active_claim is not None:
                await self._reconcile_claim_after_abort(claim=active_claim, active_cycle=active_cycle, error_code='checkpoint_apply_interrupted')
            snapshot = await self.repositories.snapshots.get(cycle_id)
            return CheckpointOutcome(checkpoint=checkpoint, action=CheckpointAction.INTERRUPT, context_revision_id=snapshot.active_context_revision_id if snapshot is not None else None, applied_through_cycle_sequence=snapshot.applied_through_cycle_sequence if snapshot is not None else 0, reason_code=self._managed_reason(error))

class EntryWatermarkCheckpointService(DurableContextCheckpointService):
    """Drain exactly the accepted watermark observed at checkpoint entry."""

    async def _persist_closed_context(self, *, checkpoint: CheckpointName, active_cycle: Any) -> CheckpointOutcome | None:
        active_revision = getattr(active_cycle, 'active_context_revision_id', None)
        if not active_revision:
            return None
        repositories = self.applier.repositories
        state = await repositories.sessions.get(str(active_cycle.session_id))
        snapshot = await repositories.snapshots.get(str(active_cycle.cycle_id))
        if state is None or snapshot is None or state.active_cycle_id != str(active_cycle.cycle_id) or (state.generation != int(getattr(active_cycle, 'input_runtime_generation', 0))) or (snapshot.session_id != str(active_cycle.session_id)) or (snapshot.generation != state.generation):
            return None
        active_through = int(getattr(active_cycle, 'applied_through_cycle_sequence', 0))
        if active_revision == snapshot.active_context_revision_id and active_through == snapshot.applied_through_cycle_sequence:
            try:
                validate_openai_tool_sequence(active_cycle.messages_for_llm)
            except CycleSegmentSelectionError:
                return CheckpointOutcome(checkpoint=checkpoint, action=CheckpointAction.INTERRUPT, context_revision_id=snapshot.active_context_revision_id, applied_through_cycle_sequence=snapshot.applied_through_cycle_sequence, reason_code='invalid_active_message_sequence')
            pre_status = CycleStatus.RUNNING if checkpoint in {CheckpointName.BEFORE_WAITING, CheckpointName.BEFORE_FINAL_PROCESSING, CheckpointName.BEFORE_TERMINAL_COMMIT} else None
            return await self._persist_checkpoint_snapshot(checkpoint=checkpoint, active_cycle=active_cycle, desired_status=pre_status, waiting_question=None, interruption_reason=None)
        if active_through >= snapshot.applied_through_cycle_sequence:
            return CheckpointOutcome(checkpoint=checkpoint, action=CheckpointAction.INTERRUPT, context_revision_id=snapshot.active_context_revision_id, applied_through_cycle_sequence=snapshot.applied_through_cycle_sequence, reason_code='active_context_authority_diverged')
        return None

    async def _bounded_ambiguous_reason(self, *, active_cycle: Any, after_sequence: int, through_sequence: int) -> str | None:
        for item in await self.applier.repositories.inbox.list_for_cycle(str(active_cycle.cycle_id)):
            if not after_sequence < item.cycle_sequence <= through_sequence:
                continue
            marker = await self.applier.repositories.handoffs.get(item.admission_id)
            if marker is None:
                continue
            if marker.state == RuntimeHandoffState.AMBIGUOUS:
                return 'ambiguous_runtime_handoff_requires_recovery'
            if item.state == InboxState.APPLYING and marker.state == RuntimeHandoffState.HANDED_OFF:
                return 'post_handoff_applying_claim_requires_recovery'
        return None

    async def sync_terminal_snapshot(self, *, session_id: str, cycle_id: str, generation: int, status: CycleStatus) -> CheckpointOutcome:
        """Synchronize terminal snapshot after existing compatibility succeeds."""
        if status not in {CycleStatus.DONE, CycleStatus.ERROR}:
            return CheckpointOutcome(checkpoint=CheckpointName.BEFORE_TERMINAL_COMMIT, action=CheckpointAction.INTERRUPT, reason_code='invalid_terminal_snapshot_status')
        repositories = self.applier.repositories
        for _ in range(8):
            state = await repositories.sessions.get(session_id)
            snapshot = await repositories.snapshots.get(cycle_id)
            if state is None or snapshot is None or state.active_cycle_id != cycle_id or (state.generation != generation) or (snapshot.session_id != session_id) or (snapshot.generation != generation):
                return CheckpointOutcome(checkpoint=CheckpointName.BEFORE_TERMINAL_COMMIT, action=CheckpointAction.INTERRUPT, reason_code='checkpoint_cycle_authority_mismatch')
            if state.cycle_status != status:
                return CheckpointOutcome(checkpoint=CheckpointName.BEFORE_TERMINAL_COMMIT, action=CheckpointAction.INTERRUPT, context_revision_id=snapshot.active_context_revision_id, applied_through_cycle_sequence=snapshot.applied_through_cycle_sequence, reason_code='terminal_compatibility_status_mismatch')
            if snapshot.status == status:
                return CheckpointOutcome(checkpoint=CheckpointName.BEFORE_TERMINAL_COMMIT, action=CheckpointAction.CONTINUE, context_revision_id=snapshot.active_context_revision_id, applied_through_cycle_sequence=snapshot.applied_through_cycle_sequence)
            candidate = snapshot.model_copy(update={'status': status, 'waiting_question': None, 'interruption_reason': None, 'safe_checkpoint': CheckpointName.BEFORE_TERMINAL_COMMIT, 'snapshot_revision': snapshot.snapshot_revision + 1, 'updated_at': self.applier._now()})
            candidate = ActiveCycleSnapshot.model_validate(candidate.model_dump(mode='python'))
            try:
                persisted = await repositories.snapshots.compare_and_swap(snapshot.snapshot_revision, candidate)
            except InputRuntimeConflictError:
                continue
            return CheckpointOutcome(checkpoint=CheckpointName.BEFORE_TERMINAL_COMMIT, action=CheckpointAction.CONTINUE, context_revision_id=persisted.active_context_revision_id, applied_through_cycle_sequence=persisted.applied_through_cycle_sequence)
        return CheckpointOutcome(checkpoint=CheckpointName.BEFORE_TERMINAL_COMMIT, action=CheckpointAction.INTERRUPT, reason_code='terminal_snapshot_cas_exhausted')

    async def _run_checkpoint_impl(self, *, checkpoint: CheckpointName, active_cycle: Any, desired_status: CycleStatus | None=None, waiting_question: str | None=None, interruption_reason: str | None=None, apply_input: bool=True, terminal_sync: bool=False) -> CheckpointOutcome:
        repositories = self.applier.repositories
        session_id = str(active_cycle.session_id)
        cycle_id = str(active_cycle.cycle_id)
        generation = int(getattr(active_cycle, 'input_runtime_generation', 0))
        entry_state = await repositories.sessions.get(session_id)
        entry_accepted = entry_state.active_cycle_accepted_through_sequence if entry_state is not None and entry_state.active_cycle_id == cycle_id and (entry_state.generation == generation) else None
        closed = await self._persist_closed_context(checkpoint=checkpoint, active_cycle=active_cycle)
        if closed is not None and closed.action == CheckpointAction.INTERRUPT:
            return closed
        input_batch_id = str(getattr(active_cycle, 'original_input_batch_id', '') or '')
        if not getattr(active_cycle, 'active_context_revision_id', None):
            if not input_batch_id:
                return CheckpointOutcome(checkpoint=checkpoint, action=CheckpointAction.INTERRUPT, reason_code='initial_input_identity_unavailable')
            initial = await self.ensure_initial_context(checkpoint=checkpoint, active_cycle=active_cycle, input_batch_id=input_batch_id)
            if initial.action == CheckpointAction.INTERRUPT:
                return initial
        state = await repositories.sessions.get(session_id)
        snapshot = await repositories.snapshots.get(cycle_id)
        if state is None or snapshot is None or state.active_cycle_id != cycle_id or (state.generation != generation) or (snapshot.session_id != session_id) or (snapshot.generation != generation):
            return CheckpointOutcome(checkpoint=checkpoint, action=CheckpointAction.INTERRUPT, reason_code='checkpoint_cycle_authority_mismatch')
        target_accepted = entry_accepted if entry_accepted is not None else state.active_cycle_accepted_through_sequence
        if snapshot.applied_through_cycle_sequence > target_accepted:
            return CheckpointOutcome(checkpoint=checkpoint, action=CheckpointAction.INTERRUPT, context_revision_id=snapshot.active_context_revision_id, applied_through_cycle_sequence=snapshot.applied_through_cycle_sequence, reason_code='snapshot_watermark_exceeds_checkpoint_entry')
        now = self.applier._now()
        await self.applier._mark_snapshot_applied_records(snapshot, now=now)
        state = await self.applier._advance_session_authority(state=state, context_revision_id=snapshot.active_context_revision_id, applied_through=snapshot.applied_through_cycle_sequence, now=now)
        self.applier._install_snapshot(active_cycle, snapshot)
        interrupted = await self._reconcile_expired_claims(cycle_id=cycle_id, generation=generation, checkpoint=checkpoint)
        if interrupted is not None:
            return interrupted
        applied_through = snapshot.applied_through_cycle_sequence
        revision_id = snapshot.active_context_revision_id
        applied_ids: list[str] = []
        while apply_input and applied_through < target_accepted:
            reason = await self._bounded_ambiguous_reason(active_cycle=active_cycle, after_sequence=applied_through, through_sequence=target_accepted)
            if reason:
                return CheckpointOutcome(checkpoint=checkpoint, action=CheckpointAction.INTERRUPT, context_revision_id=revision_id, applied_through_cycle_sequence=applied_through, reason_code=reason)
            outcome = await self.applier.apply_pending_input(session_id=session_id, cycle_id=cycle_id, generation=generation, checkpoint=checkpoint, active_cycle=active_cycle, through_sequence=target_accepted)
            if outcome.action == CheckpointAction.INTERRUPT:
                return outcome
            if outcome.action != CheckpointAction.INPUT_APPLIED:
                return CheckpointOutcome(checkpoint=checkpoint, action=CheckpointAction.INTERRUPT, context_revision_id=outcome.context_revision_id, applied_through_cycle_sequence=outcome.applied_through_cycle_sequence, reason_code='accepted_input_range_unavailable')
            applied_ids.extend(outcome.applied_input_batch_ids)
            revision_id = outcome.context_revision_id
            applied_through = outcome.applied_through_cycle_sequence
        if applied_ids:
            return CheckpointOutcome(checkpoint=checkpoint, action=CheckpointAction.INPUT_APPLIED, context_revision_id=revision_id, applied_through_cycle_sequence=applied_through, applied_input_batch_ids=tuple(applied_ids))
        effective_status = desired_status
        if checkpoint == CheckpointName.BEFORE_TERMINAL_COMMIT and (not terminal_sync):
            effective_status = CycleStatus.RUNNING
        return await self._persist_checkpoint_snapshot(checkpoint=checkpoint, active_cycle=active_cycle, desired_status=effective_status, waiting_question=waiting_question, interruption_reason=interruption_reason)

    async def run_checkpoint(self, *, checkpoint: CheckpointName, active_cycle: Any, desired_status: CycleStatus | None=None, waiting_question: str | None=None, interruption_reason: str | None=None, apply_input: bool=True, terminal_sync: bool=False) -> CheckpointOutcome:
        try:
            return await self._run_checkpoint_impl(checkpoint=checkpoint, active_cycle=active_cycle, desired_status=desired_status, waiting_question=waiting_question, interruption_reason=interruption_reason, apply_input=apply_input, terminal_sync=terminal_sync)
        except (InputRuntimeConflictError, CycleSegmentSelectionError) as error:
            snapshot = await self.applier.repositories.snapshots.get(str(active_cycle.cycle_id))
            return CheckpointOutcome(checkpoint=checkpoint, action=CheckpointAction.INTERRUPT, context_revision_id=snapshot.active_context_revision_id if snapshot is not None else None, applied_through_cycle_sequence=snapshot.applied_through_cycle_sequence if snapshot is not None else 0, reason_code=CancellationSafeCycleInputApplier._managed_reason(error))
