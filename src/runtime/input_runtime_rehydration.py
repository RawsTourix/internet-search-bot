"""IR-8 durable ActiveCycleSnapshot -> process-local ActiveAgentCycle boundary."""

from __future__ import annotations

import json
from typing import Any

from ..input_runtime.models import ActiveCycleSnapshot
from .cycle import ActiveAgentCycle


def _original_user_message_index(messages: list[dict[str, Any]]) -> int:
    candidates: list[int] = []
    for index, message in enumerate(messages):
        if message.get("role") != "user":
            continue
        candidates.append(index)
        content = message.get("content")
        if not isinstance(content, str):
            continue
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict) and payload.get("type") in {
            "user_request",
            "agent_input_batch",
        }:
            return index
    if candidates:
        return candidates[0]
    raise ValueError("snapshot has no original user message")


def rehydrate_active_agent_cycle(snapshot: ActiveCycleSnapshot) -> ActiveAgentCycle:
    """Recreate only state that is proven by the durable snapshot.

    Snapshot validation belongs to the recovery coordinator.  This function never
    invents working-memory/planning/artifact objects for durable refs it cannot
    resolve; those snapshots are classified non-resumable before reaching here.
    """

    messages = [dict(item) for item in snapshot.messages_for_llm]
    cycle = ActiveAgentCycle(
        cycle_id=snapshot.cycle_id,
        session_id=snapshot.session_id,
        original_user_request=snapshot.original_user_request,
        messages_for_llm=messages,
        cycle_trace=[dict(item) for item in snapshot.cycle_trace],
        original_user_message_index=_original_user_message_index(messages),
        status=snapshot.status.value,
        waiting_question=snapshot.waiting_question,
        interruption_reason=snapshot.interruption_reason,
        result_refs=list(snapshot.result_refs),
        artifact_refs=list(snapshot.artifact_refs),
        read_artifact_refs=list(snapshot.read_artifact_refs),
        original_input_batch_id=snapshot.original_input_batch_id,
        active_plan_id=snapshot.active_plan_id,
        active_plan_revision=snapshot.active_plan_revision,
        active_plan_node_id=snapshot.active_plan_node_id,
        input_runtime_generation=snapshot.generation,
        active_context_revision_id=snapshot.active_context_revision_id,
        applied_input_batch_ids=list(snapshot.applied_input_batch_ids),
        applied_through_cycle_sequence=snapshot.applied_through_cycle_sequence,
        input_runtime_safe_checkpoint=snapshot.safe_checkpoint.value,
        input_runtime_snapshot_revision=snapshot.snapshot_revision,
    )
    return cycle
