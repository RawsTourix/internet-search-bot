"""Domain service for revisioned DAG planning commands."""

from __future__ import annotations

from typing import Iterable

from .config import PlanningConfigType
from .errors import PlanAccessError, PlanValidationError
from .interfaces import PlanStore
from .models import (
    ActivePlanState,
    AddPlanNodeInput,
    AgentPlan,
    CreatePlanNodeInput,
    PlanNode,
    PlanNodeKind,
    PlanNodeStatus,
    PlanNodeTransition,
    PlanStatus,
    new_plan_id,
    new_plan_node_id,
    utc_now,
)
from .validation import (
    build_active_plan_state,
    dependant_node_ids,
    ready_nodes,
    validate_plan,
)


class PlanningService:
    """Apply validated plan commands and persist exact revisions."""

    def __init__(self, *, store: PlanStore, config: PlanningConfigType) -> None:
        self.store = store
        self.config = config

    async def create_plan(
        self,
        *,
        session_id: str,
        cycle_id: str,
        goal: str,
        strategy: str | None,
        nodes: list[CreatePlanNodeInput],
    ) -> tuple[AgentPlan, dict[str, str]]:
        if not self.config.enabled:
            raise PlanValidationError(
                "planning_disabled",
                "DAG planning is disabled by configuration.",
                retryable=False,
            )
        active_plans = [
            item
            for item in await self.store.list_cycle_plans(cycle_id)
            if item.status == PlanStatus.ACTIVE
        ]
        if active_plans:
            raise PlanValidationError(
                "active_plan_exists",
                "Current cycle already has an active plan.",
                details={"plan_id": active_plans[-1].plan_id},
            )
        if not nodes:
            raise PlanValidationError(
                "empty_plan",
                "Plan must contain at least one node.",
            )
        if len(nodes) > self.config.max_nodes:
            raise PlanValidationError(
                "too_many_nodes",
                "Plan exceeds the configured node limit.",
                details={"max_nodes": self.config.max_nodes},
            )

        client_keys = [node.client_key for node in nodes]
        if len(client_keys) != len(set(client_keys)):
            raise PlanValidationError(
                "duplicate_client_key",
                "Plan create request contains duplicate client keys.",
            )

        node_id_map = {
            client_key: new_plan_node_id() for client_key in client_keys
        }
        now = utc_now()
        plan_nodes: list[PlanNode] = []
        known_keys = set(client_keys)
        for node in nodes:
            unknown_dependencies = [
                key for key in node.depends_on if key not in known_keys
            ]
            if unknown_dependencies:
                raise PlanValidationError(
                    "dependency_not_found",
                    "Node references an unknown client-key dependency.",
                    details={
                        "client_key": node.client_key,
                        "dependencies": unknown_dependencies,
                    },
                )
            plan_nodes.append(
                PlanNode(
                    node_id=node_id_map[node.client_key],
                    key=node.client_key,
                    title=node.title,
                    objective=node.objective,
                    kind=node.kind,
                    depends_on=[node_id_map[key] for key in node.depends_on],
                    success_criteria=node.success_criteria,
                    created_at=now,
                    updated_at=now,
                )
            )

        plan = AgentPlan(
            plan_id=new_plan_id(),
            session_id=session_id,
            cycle_id=cycle_id,
            goal=goal,
            strategy=strategy,
            status=PlanStatus.ACTIVE,
            revision=1,
            nodes=plan_nodes,
            created_at=now,
            updated_at=now,
        )
        validate_plan(plan, self.config)
        return await self.store.create_plan(plan), node_id_map

    async def get_plan(
        self,
        *,
        session_id: str,
        cycle_id: str,
        plan_id: str,
        revision: int | None = None,
    ) -> AgentPlan:
        plan = await self.store.get_plan(plan_id, revision=revision)
        self._ensure_access(plan, session_id=session_id, cycle_id=cycle_id)
        return plan

    async def get_active_state(
        self,
        *,
        session_id: str,
        cycle_id: str,
        plan_id: str,
    ) -> ActivePlanState:
        plan = await self.get_plan(
            session_id=session_id,
            cycle_id=cycle_id,
            plan_id=plan_id,
        )
        return self._state(plan)

    async def add_nodes(
        self,
        *,
        session_id: str,
        cycle_id: str,
        plan_id: str,
        expected_revision: int,
        nodes: list[AddPlanNodeInput],
    ) -> tuple[AgentPlan, dict[str, str]]:
        if not nodes:
            raise PlanValidationError(
                "empty_node_batch",
                "At least one node is required.",
            )
        plan = await self._load_mutable_plan(
            session_id=session_id,
            cycle_id=cycle_id,
            plan_id=plan_id,
        )
        if len(plan.nodes) + len(nodes) > self.config.max_nodes:
            raise PlanValidationError(
                "too_many_nodes",
                "Plan exceeds the configured node limit.",
                details={"max_nodes": self.config.max_nodes},
            )

        existing_keys = {node.key for node in plan.nodes}
        batch_keys = [node.client_key for node in nodes]
        if len(batch_keys) != len(set(batch_keys)):
            raise PlanValidationError(
                "duplicate_client_key",
                "Node batch contains duplicate client keys.",
            )
        conflicting_keys = sorted(existing_keys.intersection(batch_keys))
        if conflicting_keys:
            raise PlanValidationError(
                "duplicate_node_key",
                "Node key already exists in the plan.",
                details={"keys": conflicting_keys},
            )

        existing_ids = {node.node_id for node in plan.nodes}
        node_id_map = {key: new_plan_node_id() for key in batch_keys}
        batch_key_set = set(batch_keys)
        now = utc_now()
        new_nodes: list[PlanNode] = []
        for node in nodes:
            unknown_ids = [
                value
                for value in node.depends_on_node_ids
                if value not in existing_ids
            ]
            unknown_keys = [
                value
                for value in node.depends_on_client_keys
                if value not in batch_key_set
            ]
            if unknown_ids or unknown_keys:
                raise PlanValidationError(
                    "dependency_not_found",
                    "New node references an unknown dependency.",
                    details={
                        "client_key": node.client_key,
                        "unknown_node_ids": unknown_ids,
                        "unknown_client_keys": unknown_keys,
                    },
                )
            dependencies = list(node.depends_on_node_ids) + [
                node_id_map[key] for key in node.depends_on_client_keys
            ]
            new_nodes.append(
                PlanNode(
                    node_id=node_id_map[node.client_key],
                    key=node.client_key,
                    title=node.title,
                    objective=node.objective,
                    kind=node.kind,
                    depends_on=dependencies,
                    success_criteria=node.success_criteria,
                    created_at=now,
                    updated_at=now,
                )
            )

        candidate = self._next_revision(
            plan,
            nodes=[*plan.nodes, *new_nodes],
            updated_at=now,
        )
        saved = await self.store.save_revision(
            candidate,
            expected_revision=expected_revision,
        )
        return saved, node_id_map

    async def update_node(
        self,
        *,
        session_id: str,
        cycle_id: str,
        plan_id: str,
        expected_revision: int,
        node_id: str,
        title: str | None = None,
        objective: str | None = None,
        kind: PlanNodeKind | None = None,
        success_criteria: list[str] | None = None,
        depends_on: list[str] | None = None,
    ) -> AgentPlan:
        if all(
            value is None
            for value in (title, objective, kind, success_criteria, depends_on)
        ):
            raise PlanValidationError(
                "empty_node_update",
                "At least one node field must be updated.",
            )
        plan = await self._load_mutable_plan(
            session_id=session_id,
            cycle_id=cycle_id,
            plan_id=plan_id,
        )
        node_index, node = self._find_node(plan, node_id)
        if node.status != PlanNodeStatus.PENDING or node.attempt_count != 0:
            raise PlanValidationError(
                "started_node_not_editable",
                "Only an unstarted pending node may be structurally edited.",
                details={"node_id": node_id},
            )
        known_ids = {item.node_id for item in plan.nodes}
        if depends_on is not None:
            unknown = [value for value in depends_on if value not in known_ids]
            if unknown:
                raise PlanValidationError(
                    "dependency_not_found",
                    "Updated node references an unknown dependency.",
                    details={"dependencies": unknown},
                )

        now = utc_now()
        updated_node = node.model_copy(
            update={
                "title": title if title is not None else node.title,
                "objective": objective if objective is not None else node.objective,
                "kind": kind if kind is not None else node.kind,
                "success_criteria": (
                    success_criteria
                    if success_criteria is not None
                    else node.success_criteria
                ),
                "depends_on": depends_on if depends_on is not None else node.depends_on,
                "updated_at": now,
            }
        )
        nodes_copy = list(plan.nodes)
        nodes_copy[node_index] = PlanNode.model_validate(updated_node.model_dump())
        candidate = self._next_revision(plan, nodes=nodes_copy, updated_at=now)
        return await self.store.save_revision(
            candidate,
            expected_revision=expected_revision,
        )

    async def transition_node(
        self,
        *,
        session_id: str,
        cycle_id: str,
        plan_id: str,
        expected_revision: int,
        node_id: str,
        transition: PlanNodeTransition,
        outcome_summary: str | None,
        reason: str | None,
        result_refs: list[str],
        artifact_refs: list[str],
        runtime_result_refs: Iterable[str],
        runtime_artifact_refs: Iterable[str],
    ) -> AgentPlan:
        plan = await self._load_mutable_plan(
            session_id=session_id,
            cycle_id=cycle_id,
            plan_id=plan_id,
        )
        node_index, node = self._find_node(plan, node_id)
        self._validate_runtime_refs(
            result_refs=result_refs,
            artifact_refs=artifact_refs,
            runtime_result_refs=runtime_result_refs,
            runtime_artifact_refs=runtime_artifact_refs,
        )
        now = utc_now()
        update = self._transition_update(
            plan=plan,
            node=node,
            transition=transition,
            outcome_summary=outcome_summary,
            reason=reason,
            result_refs=result_refs,
            artifact_refs=artifact_refs,
            now=now,
        )
        updated_node = PlanNode.model_validate(
            node.model_copy(update=update).model_dump()
        )
        nodes_copy = list(plan.nodes)
        nodes_copy[node_index] = updated_node
        next_status = plan.status
        if all(
            item.status in {PlanNodeStatus.DONE, PlanNodeStatus.SKIPPED}
            for item in nodes_copy
        ):
            next_status = PlanStatus.COMPLETED

        candidate = self._next_revision(
            plan,
            nodes=nodes_copy,
            status=next_status,
            updated_at=now,
        )
        return await self.store.save_revision(
            candidate,
            expected_revision=expected_revision,
        )

    async def remove_node(
        self,
        *,
        session_id: str,
        cycle_id: str,
        plan_id: str,
        expected_revision: int,
        node_id: str,
    ) -> AgentPlan:
        plan = await self._load_mutable_plan(
            session_id=session_id,
            cycle_id=cycle_id,
            plan_id=plan_id,
        )
        _, node = self._find_node(plan, node_id)
        if node.status != PlanNodeStatus.PENDING or node.attempt_count != 0:
            raise PlanValidationError(
                "started_node_not_removable",
                "Only an unstarted pending node may be removed.",
                details={"node_id": node_id},
            )
        dependants = dependant_node_ids(plan, node_id)
        if dependants:
            raise PlanValidationError(
                "node_has_dependants",
                "Node cannot be removed while other nodes depend on it.",
                details={"dependant_node_ids": dependants},
            )
        remaining = [item for item in plan.nodes if item.node_id != node_id]
        if not remaining:
            raise PlanValidationError(
                "empty_plan",
                "Removing the node would leave an empty plan.",
            )
        now = utc_now()
        candidate = self._next_revision(
            plan,
            nodes=remaining,
            updated_at=now,
        )
        return await self.store.save_revision(
            candidate,
            expected_revision=expected_revision,
        )

    async def cancel_plan(
        self,
        *,
        session_id: str,
        cycle_id: str,
        plan_id: str,
        expected_revision: int,
        reason: str,
    ) -> AgentPlan:
        plan = await self._load_mutable_plan(
            session_id=session_id,
            cycle_id=cycle_id,
            plan_id=plan_id,
        )
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise PlanValidationError(
                "missing_reason",
                "Cancelling a plan requires a reason.",
            )
        now = utc_now()
        candidate = self._next_revision(
            plan,
            status=PlanStatus.CANCELLED,
            cancellation_reason=normalized_reason,
            updated_at=now,
        )
        return await self.store.save_revision(
            candidate,
            expected_revision=expected_revision,
        )

    def _state(self, plan: AgentPlan) -> ActivePlanState:
        return build_active_plan_state(
            plan,
            max_ready_nodes=self.config.max_ready_nodes_in_context,
        )

    async def _load_mutable_plan(
        self,
        *,
        session_id: str,
        cycle_id: str,
        plan_id: str,
    ) -> AgentPlan:
        plan = await self.get_plan(
            session_id=session_id,
            cycle_id=cycle_id,
            plan_id=plan_id,
        )
        if plan.status != PlanStatus.ACTIVE:
            raise PlanValidationError(
                "terminal_plan_mutation",
                "Completed or cancelled plan cannot be mutated.",
                retryable=False,
                details={"status": plan.status.value},
            )
        return plan

    @staticmethod
    def _ensure_access(
        plan: AgentPlan,
        *,
        session_id: str,
        cycle_id: str,
    ) -> None:
        if plan.session_id != session_id or plan.cycle_id != cycle_id:
            raise PlanAccessError("Plan does not belong to the current cycle")

    @staticmethod
    def _find_node(plan: AgentPlan, node_id: str) -> tuple[int, PlanNode]:
        for index, node in enumerate(plan.nodes):
            if node.node_id == node_id:
                return index, node
        raise PlanValidationError(
            "node_not_found",
            "Plan node was not found.",
            details={"node_id": node_id},
        )

    def _next_revision(self, plan: AgentPlan, **updates) -> AgentPlan:
        candidate = plan.model_copy(
            deep=True,
            update={"revision": plan.revision + 1, **updates},
        )
        candidate = AgentPlan.model_validate(candidate.model_dump())
        validate_plan(candidate, self.config)
        return candidate

    @staticmethod
    def _validate_runtime_refs(
        *,
        result_refs: list[str],
        artifact_refs: list[str],
        runtime_result_refs: Iterable[str],
        runtime_artifact_refs: Iterable[str],
    ) -> None:
        known_results = set(runtime_result_refs)
        known_artifacts = set(runtime_artifact_refs)
        unknown_results = sorted(set(result_refs) - known_results)
        unknown_artifacts = sorted(set(artifact_refs) - known_artifacts)
        if unknown_results:
            raise PlanValidationError(
                "unknown_result_ref",
                "Node command references an unknown stored result.",
                details={"result_refs": unknown_results},
            )
        if unknown_artifacts:
            raise PlanValidationError(
                "unknown_artifact_ref",
                "Node command references an unknown artifact.",
                details={"artifact_refs": unknown_artifacts},
            )

    @staticmethod
    def _transition_update(
        *,
        plan: AgentPlan,
        node: PlanNode,
        transition: PlanNodeTransition,
        outcome_summary: str | None,
        reason: str | None,
        result_refs: list[str],
        artifact_refs: list[str],
        now,
    ) -> dict:
        normalized_outcome = outcome_summary.strip() if outcome_summary else None
        normalized_reason = reason.strip() if reason else None
        forbidden_payload = normalized_outcome or normalized_reason or result_refs or artifact_refs

        if transition == PlanNodeTransition.START:
            if forbidden_payload:
                raise PlanValidationError(
                    "invalid_transition_payload",
                    "start does not accept outcome, reason or refs.",
                )
            ready_ids = {item.node_id for item in ready_nodes(plan)}
            if node.status != PlanNodeStatus.PENDING or node.node_id not in ready_ids:
                raise PlanValidationError(
                    "node_not_ready",
                    "Only a ready pending node may be started.",
                    details={"node_id": node.node_id},
                )
            if any(
                item.status == PlanNodeStatus.IN_PROGRESS
                for item in plan.nodes
                if item.node_id != node.node_id
            ):
                raise PlanValidationError(
                    "another_node_in_progress",
                    "Only one node may be in progress in v0.4.",
                )
            return {
                "status": PlanNodeStatus.IN_PROGRESS,
                "attempt_count": node.attempt_count + 1,
                "started_at": now,
                "finished_at": None,
                "outcome_summary": None,
                "status_reason": None,
                "result_refs": [],
                "artifact_refs": [],
                "updated_at": now,
            }

        if transition == PlanNodeTransition.COMPLETE:
            if node.status != PlanNodeStatus.IN_PROGRESS:
                raise PlanValidationError(
                    "invalid_status_transition",
                    "Only an in-progress node may be completed.",
                )
            if not normalized_outcome:
                raise PlanValidationError(
                    "missing_outcome_summary",
                    "Completing a node requires an outcome summary.",
                )
            return {
                "status": PlanNodeStatus.DONE,
                "outcome_summary": normalized_outcome,
                "status_reason": normalized_reason,
                "result_refs": result_refs,
                "artifact_refs": artifact_refs,
                "finished_at": now,
                "updated_at": now,
            }

        if transition in {PlanNodeTransition.BLOCK, PlanNodeTransition.FAIL}:
            if node.status != PlanNodeStatus.IN_PROGRESS:
                raise PlanValidationError(
                    "invalid_status_transition",
                    "Only an in-progress node may be blocked or failed.",
                )
            if not normalized_reason:
                raise PlanValidationError(
                    "missing_reason",
                    "Blocking or failing a node requires a reason.",
                )
            status = (
                PlanNodeStatus.BLOCKED
                if transition == PlanNodeTransition.BLOCK
                else PlanNodeStatus.FAILED
            )
            return {
                "status": status,
                "outcome_summary": normalized_outcome,
                "status_reason": normalized_reason,
                "result_refs": result_refs,
                "artifact_refs": artifact_refs,
                "finished_at": now if status == PlanNodeStatus.FAILED else None,
                "updated_at": now,
            }

        if transition == PlanNodeTransition.SKIP:
            if node.status not in {
                PlanNodeStatus.PENDING,
                PlanNodeStatus.IN_PROGRESS,
                PlanNodeStatus.BLOCKED,
                PlanNodeStatus.FAILED,
            }:
                raise PlanValidationError(
                    "invalid_status_transition",
                    "Current node status cannot be skipped.",
                )
            if normalized_outcome or result_refs or artifact_refs:
                raise PlanValidationError(
                    "invalid_transition_payload",
                    "skip accepts only a reason.",
                )
            if not normalized_reason:
                raise PlanValidationError(
                    "missing_reason",
                    "Skipping a node requires a reason.",
                )
            return {
                "status": PlanNodeStatus.SKIPPED,
                "outcome_summary": None,
                "status_reason": normalized_reason,
                "result_refs": [],
                "artifact_refs": [],
                "finished_at": now,
                "updated_at": now,
            }

        if transition == PlanNodeTransition.RETRY:
            if forbidden_payload:
                raise PlanValidationError(
                    "invalid_transition_payload",
                    "retry does not accept outcome, reason or refs.",
                )
            if node.status not in {PlanNodeStatus.BLOCKED, PlanNodeStatus.FAILED}:
                raise PlanValidationError(
                    "invalid_status_transition",
                    "Only blocked or failed node may be retried.",
                )
            return {
                "status": PlanNodeStatus.PENDING,
                "outcome_summary": None,
                "status_reason": None,
                "result_refs": [],
                "artifact_refs": [],
                "started_at": None,
                "finished_at": None,
                "updated_at": now,
            }

        raise PlanValidationError(
            "unknown_transition",
            "Unknown node transition.",
            retryable=False,
        )
