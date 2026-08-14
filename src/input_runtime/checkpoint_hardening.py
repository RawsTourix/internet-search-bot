"""Hardening for preserving closed runtime context at IR-4 checkpoints."""

from __future__ import annotations

from typing import Any

from ..memory import validate_openai_tool_sequence
from .checkpoints import InputRuntimeCheckpointService
from .models import CheckpointAction, CheckpointName, CheckpointOutcome, CycleStatus


class DurableContextCheckpointService(InputRuntimeCheckpointService):
    """Persist the closed in-memory block before snapshot reconciliation.

    A matching context revision/watermark means the active process may contain
    newer, protocol-complete LLM or tool messages that have not yet reached the
    snapshot.  They are persisted first.  Snapshot installation remains the
    recovery path only when durable semantic authority is ahead of memory.
    """

    async def run_checkpoint(
        self,
        *,
        checkpoint: CheckpointName,
        active_cycle: Any,
        desired_status: CycleStatus | None = None,
        waiting_question: str | None = None,
        interruption_reason: str | None = None,
        apply_input: bool = True,
    ) -> CheckpointOutcome:
        active_revision = getattr(
            active_cycle,
            "active_context_revision_id",
            None,
        )
        if active_revision:
            repositories = self.applier.repositories
            state = await repositories.sessions.get(str(active_cycle.session_id))
            snapshot = await repositories.snapshots.get(str(active_cycle.cycle_id))
            if (
                state is not None
                and snapshot is not None
                and state.active_cycle_id == str(active_cycle.cycle_id)
                and state.generation
                == int(getattr(active_cycle, "input_runtime_generation", 0))
                and snapshot.session_id == str(active_cycle.session_id)
                and snapshot.generation == state.generation
            ):
                active_through = int(
                    getattr(
                        active_cycle,
                        "applied_through_cycle_sequence",
                        0,
                    )
                )
                if (
                    active_revision == snapshot.active_context_revision_id
                    and active_through
                    == snapshot.applied_through_cycle_sequence
                ):
                    try:
                        validate_openai_tool_sequence(
                            active_cycle.messages_for_llm
                        )
                    except Exception:
                        return CheckpointOutcome(
                            checkpoint=checkpoint,
                            action=CheckpointAction.INTERRUPT,
                            context_revision_id=(
                                snapshot.active_context_revision_id
                            ),
                            applied_through_cycle_sequence=(
                                snapshot.applied_through_cycle_sequence
                            ),
                            reason_code="invalid_active_message_sequence",
                        )
                    synced = await self._persist_checkpoint_snapshot(
                        checkpoint=checkpoint,
                        active_cycle=active_cycle,
                        desired_status=None,
                        waiting_question=None,
                        interruption_reason=None,
                    )
                    if synced.action == CheckpointAction.INTERRUPT:
                        return synced
                elif active_through >= snapshot.applied_through_cycle_sequence:
                    return CheckpointOutcome(
                        checkpoint=checkpoint,
                        action=CheckpointAction.INTERRUPT,
                        context_revision_id=(
                            snapshot.active_context_revision_id
                        ),
                        applied_through_cycle_sequence=(
                            snapshot.applied_through_cycle_sequence
                        ),
                        reason_code="active_context_authority_diverged",
                    )

        return await super().run_checkpoint(
            checkpoint=checkpoint,
            active_cycle=active_cycle,
            desired_status=desired_status,
            waiting_question=waiting_question,
            interruption_reason=interruption_reason,
            apply_input=apply_input,
        )
