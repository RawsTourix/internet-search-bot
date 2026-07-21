import tempfile
import unittest
from pathlib import Path

from src.mcp.manager_context import ManagerToolContext
from src.mcp.mcp_client import SessionState
from src.planning import PlanningConfigType, create_planning_services
from src.planning.safe_tools import SafePlanningToolController
from src.runtime import ActiveAgentCycle
from src.storage import StorageConfigType


class PlanReconciliationAccountingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        services = create_planning_services(
            storage_config=StorageConfigType(root_dir=str(root / "storage")),
            planning_config=PlanningConfigType(),
        )
        self.controller = SafePlanningToolController(
            services.planning_service
        )
        self.cycle = ActiveAgentCycle(
            cycle_id="cycle-1",
            session_id="session-1",
            original_user_request="Task",
            messages_for_llm=[],
            cycle_trace=[],
            original_user_message_index=0,
        )
        self.context = ManagerToolContext(
            session_id="session-1",
            cycle_id="cycle-1",
            active_cycle=self.cycle,
            session_state=SessionState(),
        )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_read_does_not_reset_reconciliation_attempts(self):
        created = await self.controller.execute(
            "agent_plan_create",
            {
                "goal": "Task",
                "nodes": [
                    {
                        "client_key": "work",
                        "title": "Work",
                        "objective": "Complete work",
                        "kind": "execute",
                        "depends_on": [],
                        "success_criteria": [],
                    }
                ],
            },
            self.context,
        )
        self.assertEqual(created.payload["type"], "plan_created")
        self.cycle.plan_reconciliation_attempts = 1
        outcome = await self.controller.execute(
            "agent_plan_get",
            {"view": "summary"},
            self.context,
        )
        self.assertEqual(outcome.payload["type"], "active_plan_state")
        self.assertEqual(self.cycle.plan_reconciliation_attempts, 1)

    async def test_successful_mutation_resets_reconciliation_attempts(self):
        created = await self.controller.execute(
            "agent_plan_create",
            {
                "goal": "Task",
                "nodes": [
                    {
                        "client_key": "work",
                        "title": "Work",
                        "objective": "Complete work",
                        "kind": "execute",
                        "depends_on": [],
                        "success_criteria": [],
                    }
                ],
            },
            self.context,
        )
        self.cycle.plan_reconciliation_attempts = 1
        outcome = await self.controller.execute(
            "agent_plan_transition_node",
            {
                "plan_id": created.payload["plan_id"],
                "expected_revision": 1,
                "node_id": created.payload["node_id_map"]["work"],
                "transition": "start",
                "result_refs": [],
                "artifact_refs": [],
            },
            self.context,
        )
        self.assertEqual(outcome.payload["type"], "plan_node_transitioned")
        self.assertEqual(self.cycle.plan_reconciliation_attempts, 0)
