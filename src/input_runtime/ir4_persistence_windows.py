"""IR-4 durable claim acquisition and terminal persistence windows."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any

from ..memory import CycleSegmentSelectionError
from .applier import CycleInputApplier
from .errors import InputRuntimeConflictError
from .factory import InputRuntimeRepositories
from .ir4_checkpoint_contracts import (
    CancellationSafeCycleInputApplier,
    EntryBoundInboxRepository,
    ManagedCommittedBatchReader,
    PreclaimedInboxRepository,
)
from .models import (
    CheckpointAction,
    CheckpointName,
    CheckpointOutcome,
    ClaimedInboxRange,
)


logger = logging.getLogger(__name__)


class DurableClaimCycleInputApplier(CancellationSafeCycleInputApplier):
    """Acquire the durable FIFO claim inside the cancellation boundary."""

    @staticmethod
    def _log_cancelled_phase_failure(label: str, error: BaseException) -> None:
        logger.error(
            "%s failed while preserving the original checkpoint cancellation",
            label,
            exc_info=(type(error), error, error.__traceback__),
        )

    @classmethod
    async def _finish_task_after_cancellation(
        cls,
        task: asyncio.Task,
        *,
        label: str,
    ) -> tuple[Any | None, BaseException | None]:
        """Wait through repeated cancellation and return, rather than raise, failure."""

        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break

        try:
            return task.result(), None
        except asyncio.CancelledError as error:
            cls._log_cancelled_phase_failure(label, error)
            return None, error
        except Exception as error:
            cls._log_cancelled_phase_failure(label, error)
            return None, error

    @classmethod
    async def _finish_cleanup_after_cancellation(
        cls,
        awaitable: Any,
        *,
        label: str,
    ) -> BaseException | None:
        cleanup_task = asyncio.create_task(awaitable)
        _result, error = await cls._finish_task_after_cancellation(
            cleanup_task,
            label=label,
        )
        return error

    async def apply_pending_input(
        self,
        *,
        session_id: str,
        cycle_id: str,
        generation: int,
        checkpoint: CheckpointName,
        active_cycle: Any,
        through_sequence: int | None = None,
    ) -> CheckpointOutcome:
        if through_sequence is None:
            state = await self.repositories.sessions.get(session_id)
            if (
                state is None
                or state.active_cycle_id != cycle_id
                or state.generation != generation
            ):
                return CheckpointOutcome(
                    checkpoint=checkpoint,
                    action=CheckpointAction.INTERRUPT,
                    reason_code="checkpoint_cycle_authority_mismatch",
                )
            through_sequence = state.active_cycle_accepted_through_sequence
        if through_sequence < 0:
            return CheckpointOutcome(
                checkpoint=checkpoint,
                action=CheckpointAction.INTERRUPT,
                reason_code="invalid_checkpoint_entry_watermark",
            )

        active_claim: ClaimedInboxRange | None = None

        def remember(claim: ClaimedInboxRange | None) -> None:
            nonlocal active_claim
            if claim is not None:
                active_claim = claim

        snapshot = await self.repositories.snapshots.get(cycle_id)
        if (
            snapshot is None
            or snapshot.session_id != session_id
            or snapshot.generation != generation
        ):
            return CheckpointOutcome(
                checkpoint=checkpoint,
                action=CheckpointAction.INTERRUPT,
                reason_code="checkpoint_cycle_authority_mismatch",
            )

        bounded_inbox = EntryBoundInboxRepository(
            self.repositories.inbox,
            through_sequence=through_sequence,
            claim_observer=remember,
        )
        claim_task = asyncio.create_task(
            bounded_inbox.claim_contiguous_range(
                cycle_id,
                generation=generation,
                after_sequence=snapshot.applied_through_cycle_sequence,
                through_sequence=through_sequence,
                max_items=self.config.max_batches_per_checkpoint,
                max_bytes=self.config.max_batch_bytes_per_checkpoint,
                lease_seconds=self.config.claim_lease_seconds,
            )
        )
        try:
            preclaimed = await asyncio.shield(claim_task)
        except asyncio.CancelledError:
            preclaimed, _claim_error = await self._finish_task_after_cancellation(
                claim_task,
                label="checkpoint claim acquisition",
            )
            remember(preclaimed)
            if active_claim is not None:
                await self._finish_cleanup_after_cancellation(
                    self._reconcile_claim_after_abort(
                        claim=active_claim,
                        active_cycle=active_cycle,
                        error_code="checkpoint_claim_cancelled",
                    ),
                    label="checkpoint claim cancellation cleanup",
                )
            # Cleanup failures are logged above and must never replace the
            # cancellation that interrupted the caller.
            raise

        preclaimed_inbox = PreclaimedInboxRepository(
            self.repositories.inbox,
            claim=preclaimed,
            claim_observer=remember,
        )
        bounded_repositories: InputRuntimeRepositories = replace(
            self.repositories,
            inbox=preclaimed_inbox,
        )
        delegate = CycleInputApplier(
            config=self.config,
            repositories=bounded_repositories,
            committed_batches=ManagedCommittedBatchReader(self.committed_batches),
            clock=self.clock,
            revision_id_factory=self.revision_id_factory,
        )
        try:
            return await delegate.apply_pending_input(
                session_id=session_id,
                cycle_id=cycle_id,
                generation=generation,
                checkpoint=checkpoint,
                active_cycle=active_cycle,
            )
        except asyncio.CancelledError:
            if active_claim is not None:
                await self._finish_cleanup_after_cancellation(
                    self._reconcile_claim_after_abort(
                        claim=active_claim,
                        active_cycle=active_cycle,
                        error_code="checkpoint_apply_cancelled",
                    ),
                    label="checkpoint apply cancellation cleanup",
                )
            raise
        except (InputRuntimeConflictError, CycleSegmentSelectionError) as error:
            if active_claim is not None:
                await self._reconcile_claim_after_abort(
                    claim=active_claim,
                    active_cycle=active_cycle,
                    error_code="checkpoint_apply_interrupted",
                )
            current = await self.repositories.snapshots.get(cycle_id)
            return CheckpointOutcome(
                checkpoint=checkpoint,
                action=CheckpointAction.INTERRUPT,
                context_revision_id=(
                    current.active_context_revision_id
                    if current is not None
                    else None
                ),
                applied_through_cycle_sequence=(
                    current.applied_through_cycle_sequence
                    if current is not None
                    else 0
                ),
                reason_code=self._managed_reason(error),
            )
