"""Unified IR-4 checkpoint orchestration."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from .applier import CycleInputApplier
from .handoff import RuntimeHandoffState
from .models import (
    CheckpointAction,
    CheckpointName,
    CheckpointOutcome,
    ClaimedInboxRange,
    InboxState,
)


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

    async def _reconcile_expired_claims(
        self,
        *,
        cycle_id: str,
        generation: int,
        checkpoint: CheckpointName,
    ) -> CheckpointOutcome | None:
        now = self.applier._now()
        items = await self.applier.repositories.inbox.list_for_cycle(cycle_id)
        groups: dict[str, list[Any]] = defaultdict(list)
        for item in items:
            if (
                item.generation == generation
                and item.state in {InboxState.CLAIMED, InboxState.APPLYING}
                and item.claim_token
                and item.claim_expires_at is not None
                and item.claim_expires_at <= now
            ):
                groups[item.claim_token].append(item)

        for token, group in groups.items():
            group.sort(key=lambda item: item.cycle_sequence)
            for item in group:
                marker = await self.applier.repositories.handoffs.get(
                    item.admission_id
                )
                if marker is not None and marker.state in {
                    RuntimeHandoffState.HANDED_OFF,
                    RuntimeHandoffState.AMBIGUOUS,
                }:
                    return CheckpointOutcome(
                        checkpoint=checkpoint,
                        action=CheckpointAction.INTERRUPT,
                        reason_code=(
                            "expired_post_handoff_claim_requires_recovery"
                        ),
                    )
            claim = ClaimedInboxRange(
                cycle_id=cycle_id,
                generation=generation,
                claim_token=token,
                first_cycle_sequence=group[0].cycle_sequence,
                last_cycle_sequence=group[-1].cycle_sequence,
                items=tuple(group),
                claimed_bytes=sum(item.payload_size_bytes for item in group),
                claim_expires_at=group[0].claim_expires_at,
            )
            await self.applier.repositories.inbox.requeue_claim(
                claim,
                error_code="expired_checkpoint_claim_requeued",
            )
        return None

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

        interrupted = await self._reconcile_expired_claims(
            cycle_id=str(active_cycle.cycle_id),
            generation=int(getattr(active_cycle, "input_runtime_generation", 0)),
            checkpoint=checkpoint,
        )
        if interrupted is not None:
            return interrupted

        return await self.applier.apply_pending_input(
            session_id=str(active_cycle.session_id),
            cycle_id=str(active_cycle.cycle_id),
            generation=int(getattr(active_cycle, "input_runtime_generation", 0)),
            checkpoint=checkpoint,
            active_cycle=active_cycle,
        )
