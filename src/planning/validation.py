"""Pure DAG validation and derived-state helpers."""

from __future__ import annotations

from collections import Counter, deque

from .config import PlanningConfigType
from .errors import PlanValidationError
from .models import (
    ActivePlanState,
    AgentPlan,
    PlanNode,
    PlanNodeCounts,
    PlanNodeStatus,
    PlanNodeSummary,
    PlanStatus,
)


def validate_plan(plan: AgentPlan, config: PlanningConfigType) -> None:
    """Validate structural and lifecycle invariants for one plan revision."""
    if len(plan.nodes) > config.max_nodes:
        raise PlanValidationError(
            "too_many_nodes",
            "Plan exceeds the configured node limit.",
            details={"max_nodes": config.max_nodes},
        )

    node_by_id: dict[str, PlanNode] = {}
    key_to_id: dict[str, str] = {}
    in_progress_count = 0

    for node in plan.nodes:
        if node.node_id in node_by_id:
            raise PlanValidationError(
                "duplicate_node_id",
                "Plan contains duplicate node identifiers.",
                retryable=False,
            )
        if node.key in key_to_id:
            raise PlanValidationError(
                "duplicate_node_key",
                "Plan contains duplicate node keys.",
                details={"key": node.key},
            )
        if len(node.depends_on) > config.max_dependencies_per_node:
            raise PlanValidationError(
                "too_many_dependencies",
                "Node exceeds the configured dependency limit.",
                details={
                    "node_id": node.node_id,
                    "max_dependencies": config.max_dependencies_per_node,
                },
            )
        if len(node.success_criteria) > config.max_success_criteria_per_node:
            raise PlanValidationError(
                "too_many_success_criteria",
                "Node exceeds the configured success-criteria limit.",
                details={
                    "node_id": node.node_id,
                    "max_success_criteria": config.max_success_criteria_per_node,
                },
            )
        if len(node.title) > config.max_title_chars:
            raise PlanValidationError(
                "title_too_long",
                "Node title exceeds the configured limit.",
                details={"node_id": node.node_id},
            )
        if len(node.objective) > config.max_objective_chars:
            raise PlanValidationError(
                "objective_too_long",
                "Node objective exceeds the configured limit.",
                details={"node_id": node.node_id},
            )
        if (
            node.outcome_summary is not None
            and len(node.outcome_summary) > config.max_outcome_summary_chars
        ):
            raise PlanValidationError(
                "outcome_summary_too_long",
                "Node outcome summary exceeds the configured limit.",
                details={"node_id": node.node_id},
            )
        if node.status == PlanNodeStatus.IN_PROGRESS:
            in_progress_count += 1
        node_by_id[node.node_id] = node
        key_to_id[node.key] = node.node_id

    if in_progress_count > 1:
        raise PlanValidationError(
            "multiple_in_progress_nodes",
            "Only one node may be in progress in v0.4.",
            retryable=False,
        )

    for node in plan.nodes:
        for dependency_id in node.depends_on:
            if dependency_id not in node_by_id:
                raise PlanValidationError(
                    "dependency_not_found",
                    "Node references an unknown dependency.",
                    details={
                        "node_id": node.node_id,
                        "dependency_id": dependency_id,
                    },
                )
            if dependency_id == node.node_id:
                raise PlanValidationError(
                    "self_dependency",
                    "Node cannot depend on itself.",
                    details={"node_id": node.node_id},
                )

    _validate_acyclic(node_by_id)

    if plan.status == PlanStatus.COMPLETED and any(
        node.status not in {PlanNodeStatus.DONE, PlanNodeStatus.SKIPPED}
        for node in plan.nodes
    ):
        raise PlanValidationError(
            "completed_plan_has_unresolved_nodes",
            "Completed plan contains unresolved nodes.",
            retryable=False,
        )


def _validate_acyclic(node_by_id: dict[str, PlanNode]) -> None:
    indegree = {node_id: 0 for node_id in node_by_id}
    dependants: dict[str, list[str]] = {node_id: [] for node_id in node_by_id}

    for node in node_by_id.values():
        for dependency_id in node.depends_on:
            indegree[node.node_id] += 1
            dependants[dependency_id].append(node.node_id)

    queue = deque(sorted(node_id for node_id, degree in indegree.items() if degree == 0))
    visited = 0
    while queue:
        node_id = queue.popleft()
        visited += 1
        for dependant_id in sorted(dependants[node_id]):
            indegree[dependant_id] -= 1
            if indegree[dependant_id] == 0:
                queue.append(dependant_id)

    if visited != len(node_by_id):
        raise PlanValidationError(
            "dag_cycle_detected",
            "Plan dependencies contain a cycle.",
        )


def ready_nodes(plan: AgentPlan) -> list[PlanNode]:
    """Return deterministic ready nodes without persisting a ready status."""
    node_by_id = {node.node_id: node for node in plan.nodes}
    ready: list[PlanNode] = []
    for node in plan.nodes:
        if node.status != PlanNodeStatus.PENDING:
            continue
        if all(
            node_by_id[dependency_id].status == PlanNodeStatus.DONE
            for dependency_id in node.depends_on
        ):
            ready.append(node)
    return ready


def current_node(plan: AgentPlan) -> PlanNode | None:
    return next(
        (node for node in plan.nodes if node.status == PlanNodeStatus.IN_PROGRESS),
        None,
    )


def is_stalled(plan: AgentPlan) -> bool:
    if plan.status != PlanStatus.ACTIVE:
        return False
    unresolved = any(
        node.status not in {PlanNodeStatus.DONE, PlanNodeStatus.SKIPPED}
        for node in plan.nodes
    )
    return unresolved and current_node(plan) is None and not ready_nodes(plan)


def build_active_plan_state(
    plan: AgentPlan,
    *,
    max_ready_nodes: int,
) -> ActivePlanState:
    """Build a bounded, exact projection for the LLM runtime message."""
    counts = Counter(node.status.value for node in plan.nodes)
    ready = ready_nodes(plan)
    current = current_node(plan)

    def summary(node: PlanNode) -> PlanNodeSummary:
        return PlanNodeSummary(
            node_id=node.node_id,
            key=node.key,
            title=node.title,
            kind=node.kind,
            status=node.status,
        )

    return ActivePlanState(
        plan_id=plan.plan_id,
        revision=plan.revision,
        status=plan.status,
        goal=plan.goal,
        current_node=summary(current) if current is not None else None,
        ready_nodes=[summary(node) for node in ready[:max_ready_nodes]],
        counts=PlanNodeCounts(
            total=len(plan.nodes),
            pending=counts[PlanNodeStatus.PENDING.value],
            in_progress=counts[PlanNodeStatus.IN_PROGRESS.value],
            blocked=counts[PlanNodeStatus.BLOCKED.value],
            done=counts[PlanNodeStatus.DONE.value],
            failed=counts[PlanNodeStatus.FAILED.value],
            skipped=counts[PlanNodeStatus.SKIPPED.value],
        ),
        stalled=is_stalled(plan),
        blocked_node_ids=[
            node.node_id
            for node in plan.nodes
            if node.status == PlanNodeStatus.BLOCKED
        ],
        failed_node_ids=[
            node.node_id
            for node in plan.nodes
            if node.status == PlanNodeStatus.FAILED
        ],
        ready_nodes_truncated=len(ready) > max_ready_nodes,
    )


def dependant_node_ids(plan: AgentPlan, node_id: str) -> list[str]:
    return sorted(
        node.node_id for node in plan.nodes if node_id in node.depends_on
    )
