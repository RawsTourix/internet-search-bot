import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from src.planning import (
    AgentPlan,
    PlanNode,
    PlanNodeKind,
    PlanNodeStatus,
    PlanStatus,
    PlanningConfigType,
    build_active_plan_state,
    is_plan_id,
    is_plan_node_id,
    new_plan_id,
    new_plan_node_id,
    validate_plan,
)
from src.planning.errors import PlanValidationError


NOW = datetime.now(timezone.utc)


def make_node(
    *,
    key: str,
    depends_on=None,
    status: PlanNodeStatus = PlanNodeStatus.PENDING,
    outcome_summary: str | None = None,
    status_reason: str | None = None,
    started_at=None,
    finished_at=None,
):
    return PlanNode(
        node_id=new_plan_node_id(),
        key=key,
        title=f"Node {key}",
        objective=f"Complete {key}",
        kind=PlanNodeKind.PROCESS,
        depends_on=list(depends_on or []),
        status=status,
        outcome_summary=outcome_summary,
        status_reason=status_reason,
        created_at=NOW,
        updated_at=NOW,
        started_at=started_at,
        finished_at=finished_at,
    )


def make_plan(nodes, *, status: PlanStatus = PlanStatus.ACTIVE):
    return AgentPlan(
        plan_id=new_plan_id(),
        session_id="session-1",
        cycle_id="cycle-1",
        goal="Complete the task",
        status=status,
        revision=1,
        nodes=nodes,
        created_at=NOW,
        updated_at=NOW,
    )


class PlanningModelTests(unittest.TestCase):
    def test_generated_identifiers_are_valid(self):
        self.assertTrue(is_plan_id(new_plan_id()))
        self.assertTrue(is_plan_node_id(new_plan_node_id()))

    def test_client_key_and_timestamp_validation(self):
        with self.assertRaises(ValidationError):
            make_node(key="Invalid-Key")
        with self.assertRaises(ValidationError):
            PlanNode(
                node_id=new_plan_node_id(),
                key="valid",
                title="Title",
                objective="Objective",
                kind=PlanNodeKind.PROCESS,
                created_at=datetime.now(),
                updated_at=NOW,
            )

    def test_done_node_requires_outcome_and_timestamp(self):
        with self.assertRaises(ValidationError):
            make_node(key="done", status=PlanNodeStatus.DONE)
        node = make_node(
            key="done",
            status=PlanNodeStatus.DONE,
            outcome_summary="Completed",
            finished_at=NOW,
        )
        self.assertEqual(node.status, PlanNodeStatus.DONE)

    def test_extra_fields_are_forbidden(self):
        payload = make_node(key="a").model_dump()
        payload["extra"] = True
        with self.assertRaises(ValidationError):
            PlanNode.model_validate(payload)

    def test_valid_branching_graph_and_ready_projection(self):
        root = make_node(key="root")
        left = make_node(key="left", depends_on=[root.node_id])
        right = make_node(key="right", depends_on=[root.node_id])
        plan = make_plan([root, left, right])
        validate_plan(plan, PlanningConfigType())
        state = build_active_plan_state(plan, max_ready_nodes=5)
        self.assertEqual([item.node_id for item in state.ready_nodes], [root.node_id])
        self.assertFalse(state.stalled)

    def test_cycle_detection(self):
        first = make_node(key="first")
        second = make_node(key="second", depends_on=[first.node_id])
        first = first.model_copy(update={"depends_on": [second.node_id]})
        plan = make_plan([first, second])
        with self.assertRaises(PlanValidationError) as raised:
            validate_plan(plan, PlanningConfigType())
        self.assertEqual(raised.exception.code, "dag_cycle_detected")

    def test_skipped_dependency_does_not_make_downstream_ready(self):
        skipped = make_node(
            key="skipped",
            status=PlanNodeStatus.SKIPPED,
            status_reason="No longer required",
            finished_at=NOW,
        )
        downstream = make_node(key="downstream", depends_on=[skipped.node_id])
        plan = make_plan([skipped, downstream])
        state = build_active_plan_state(plan, max_ready_nodes=5)
        self.assertEqual(state.ready_nodes, [])
        self.assertTrue(state.stalled)

    def test_planning_config_cross_field_validation(self):
        with self.assertRaises(ValidationError):
            PlanningConfigType(max_nodes=5, max_plan_get_limit=6)
