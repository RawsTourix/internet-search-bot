import tempfile
import unittest
from pathlib import Path

from src.mcp.manager_context import ManagerToolContext
from src.mcp.mcp_client import SessionState
from src.planning import PlanningConfigType, create_planning_services
from src.planning.tools import PlanningToolController
from src.runtime import ActiveAgentCycle
from src.storage import StorageConfigType


class PlanManagerToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.services = create_planning_services(
            storage_config=StorageConfigType(root_dir=str(root / "storage")),
            planning_config=PlanningConfigType(),
        )
        self.controller = PlanningToolController(
            self.services.planning_service
        )
        self.cycle = ActiveAgentCycle(
            cycle_id="cycle-1",
            session_id="session-1",
            original_user_request="Complete a complex task",
            messages_for_llm=[
                {"role": "system", "content": "system"},
                {"role": "user", "content": "request"},
            ],
            cycle_trace=[],
            original_user_message_index=1,
        )
        self.context = ManagerToolContext(
            session_id="session-1",
            cycle_id="cycle-1",
            active_cycle=self.cycle,
            session_state=SessionState(),
        )

    def tearDown(self):
        self.temporary.cleanup()

    async def _create(self):
        outcome = await self.controller.execute(
            "agent_plan_create",
            {
                "goal": "Collect and validate data",
                "strategy": "Collect before validation",
                "nodes": [
                    {
                        "client_key": "collect",
                        "title": "Collect data",
                        "objective": "Collect source data",
                        "kind": "collect",
                        "depends_on": [],
                        "success_criteria": ["Data is available"],
                    },
                    {
                        "client_key": "validate",
                        "title": "Validate data",
                        "objective": "Validate collected data",
                        "kind": "validate",
                        "depends_on": ["collect"],
                        "success_criteria": ["Data is validated"],
                    },
                ],
            },
            self.context,
        )
        self.assertEqual(outcome.payload["type"], "plan_created")
        return outcome.payload

    async def test_create_and_get_active_summary(self):
        created = await self._create()
        self.assertEqual(self.cycle.active_plan_id, created["plan_id"])
        self.assertEqual(self.cycle.active_plan_revision, 1)
        get_outcome = await self.controller.execute(
            "agent_plan_get",
            {"view": "summary"},
            self.context,
        )
        self.assertEqual(get_outcome.payload["type"], "active_plan_state")
        self.assertEqual(get_outcome.payload["revision"], 1)

    async def test_revision_conflict_returns_fresh_state(self):
        created = await self._create()
        plan_id = created["plan_id"]
        collect_id = created["node_id_map"]["collect"]
        first = await self.controller.execute(
            "agent_plan_transition_node",
            {
                "plan_id": plan_id,
                "expected_revision": 1,
                "node_id": collect_id,
                "transition": "start",
                "result_refs": [],
                "artifact_refs": [],
            },
            self.context,
        )
        self.assertEqual(first.payload["revision"], 2)
        conflict = await self.controller.execute(
            "agent_plan_transition_node",
            {
                "plan_id": plan_id,
                "expected_revision": 1,
                "node_id": collect_id,
                "transition": "start",
                "result_refs": [],
                "artifact_refs": [],
            },
            self.context,
        )
        self.assertEqual(conflict.payload["type"], "plan_revision_conflict")
        self.assertEqual(conflict.payload["current_revision"], 2)
        self.assertEqual(self.cycle.active_plan_revision, 2)

    async def test_unknown_result_ref_is_structured_validation_error(self):
        created = await self._create()
        plan_id = created["plan_id"]
        node_id = created["node_id_map"]["collect"]
        await self.controller.execute(
            "agent_plan_transition_node",
            {
                "plan_id": plan_id,
                "expected_revision": 1,
                "node_id": node_id,
                "transition": "start",
                "result_refs": [],
                "artifact_refs": [],
            },
            self.context,
        )
        outcome = await self.controller.execute(
            "agent_plan_transition_node",
            {
                "plan_id": plan_id,
                "expected_revision": 2,
                "node_id": node_id,
                "transition": "complete",
                "outcome_summary": "Collected",
                "result_refs": ["res_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
                "artifact_refs": [],
            },
            self.context,
        )
        self.assertEqual(outcome.payload["type"], "plan_validation_error")
        self.assertEqual(outcome.payload["code"], "unknown_result_ref")

    async def test_plan_from_other_cycle_is_not_accessible(self):
        created = await self._create()
        other_cycle = ActiveAgentCycle(
            cycle_id="cycle-2",
            session_id="session-1",
            original_user_request="Other task",
            messages_for_llm=[],
            cycle_trace=[],
            original_user_message_index=0,
        )
        other_context = ManagerToolContext(
            session_id="session-1",
            cycle_id="cycle-2",
            active_cycle=other_cycle,
            session_state=SessionState(),
        )
        outcome = await self.controller.execute(
            "agent_plan_get",
            {"plan_id": created["plan_id"], "view": "summary"},
            other_context,
        )
        self.assertEqual(outcome.payload["type"], "plan_access_error")
