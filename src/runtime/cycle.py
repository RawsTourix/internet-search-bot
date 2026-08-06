"""Mutable runtime state owned by one active agent cycle."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..artifacts.runtime import ArtifactRuntimeState
    from ..memory.models import CycleWorkingMemory
    from ..memory.token_estimation import TokenUsageSnapshot
    from ..planning.models import ActivePlanState, AgentActivity


@dataclass(slots=True)
class ActiveAgentCycle:
    cycle_id: str
    session_id: str
    original_user_request: str

    messages_for_llm: list[dict[str, Any]]
    cycle_trace: list[dict[str, Any]]

    original_user_message_index: int

    working_memory: CycleWorkingMemory | None = None

    status: str = "running"
    waiting_question: str | None = None
    interruption_reason: str | None = None
    interrupted_at: float | None = None

    result_refs: list[str] = field(default_factory=list)
    artifact_refs: list[str] = field(default_factory=list)
    read_artifact_refs: list[str] = field(default_factory=list)
    artifact_candidate_refs: list[str] = field(default_factory=list)
    artifact_activations: list[dict[str, Any]] = field(default_factory=list)
    artifact_state: ArtifactRuntimeState | None = None
    original_input_batch_id: str | None = None
    blocked_artifact_batch_signatures: list[str] = field(default_factory=list)

    active_plan_id: str | None = None
    active_plan_revision: int | None = None
    active_plan_node_id: str | None = None
    active_plan_state: ActivePlanState | None = None
    activity: AgentActivity | None = None
    plan_reconciliation_attempts: int = 0

    input_runtime_generation: int = 0
    active_context_revision_id: str | None = None
    applied_input_batch_ids: list[str] = field(default_factory=list)
    applied_through_cycle_sequence: int = 0
    input_runtime_safe_checkpoint: str | None = None
    input_runtime_snapshot_revision: int = 0

    tools_used: list[str] = field(default_factory=list)
    progress_events: list[dict[str, Any]] = field(default_factory=list)

    compaction_failures: int = 0
    last_compaction_message_count: int | None = None
    last_compaction_failure_signature: tuple[Any, ...] | None = None
    compaction_retry_min_eligible_tokens: int | None = None

    token_usage_snapshot: TokenUsageSnapshot | None = None

    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


# Compatibility name for internal imports and older callers. There is only
# one dataclass model; this is intentionally an alias rather than a subclass.
AgentCycleSnapshot = ActiveAgentCycle
