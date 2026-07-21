import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.mcp.manager_context import ManagerToolContext
from src.mcp.mcp_client import SessionState
from src.planning import (
    AgentPlan,
    CreatePlanNodeInput,
    PlanNode,
    PlanNodeKind,
    PlanningConfigType,
    create_planning_services,
    new_plan_id,
    new_plan_node_id,
    validate_plan,
)
from src.planning.errors import (
    PlanStorageError,
    PlanValidationError,
)
from src.planning.hardened_store import VerifiedFileSystemPlanStore
from src.planning.safe_tools import SafePlanningToolController
from src.runtime import ActiveAgentCycle
from src.storage import StorageConfigType


NOW = datetime.now(timezone.utc)


def create_input():
    return [
        CreatePlanNodeInput(
            client_key="work",
            title="Work",
            objective="Complete the work",
            kind=PlanNodeKind.EXECUTE,
        )
    ]


def make_plan():
    return AgentPlan(
        plan_id=new_plan_id(),
        session_id="session-1",
        cycle_id="cycle-1",
        goal="Complete task",
        revision=1,
        nodes=[
            PlanNode(
                node_id=new_plan_node_id(),
                key="work",
                title="Work",
                objective="Complete the work",
                kind=PlanNodeKind.EXECUTE,
                created_at=NOW,
                updated_at=NOW,
            )
        ],
        created_at=NOW,
        updated_at=NOW,
    )


class PlanningHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.storage_config = StorageConfigType(
            root_dir=str(self.root / "storage")
        )
        self.planning_config = PlanningConfigType()

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_concurrent_create_allows_only_one_active_plan(self):
        services = create_planning_services(
            storage_config=self.storage_config,
            planning_config=self.planning_config,
        )

        async def create():
            return await services.planning_service.create_plan(
                session_id="session-1",
                cycle_id="cycle-1",
                goal="Complete task",
                strategy=None,
                nodes=create_input(),
            )

        outcomes = await asyncio.gather(
            create(),
            create(),
            return_exceptions=True,
        )
        successes = [item for item in outcomes if not isinstance(item, Exception)]
        failures = [item for item in outcomes if isinstance(item, Exception)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], PlanValidationError)
        self.assertEqual(failures[0].code, "active_plan_exists")

    async def test_revision_ownership_mismatch_is_rejected(self):
        store = VerifiedFileSystemPlanStore(
            storage_config=self.storage_config,
            planning_config=self.planning_config,
        )
        plan = make_plan()
        await store.create_plan(plan)
        revision_path = (
            store.root / plan.plan_id / "revisions" / "000001.json"
        )
        payload = json.loads(revision_path.read_text(encoding="utf-8"))
        payload["session_id"] = "other-session"
        revision_path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(PlanStorageError):
            await store.get_plan(plan.plan_id)

    async def test_safe_conflict_refresh_propagates_storage_failure(self):
        class FailingService:
            async def get_active_state(self, **kwargs):
                raise PlanStorageError("storage failed")

        controller = SafePlanningToolController(FailingService())
        cycle = ActiveAgentCycle(
            cycle_id="cycle-1",
            session_id="session-1",
            original_user_request="Task",
            messages_for_llm=[],
            cycle_trace=[],
            original_user_message_index=0,
        )
        context = ManagerToolContext(
            session_id="session-1",
            cycle_id="cycle-1",
            active_cycle=cycle,
            session_state=SessionState(),
        )
        with self.assertRaises(PlanStorageError):
            await controller._refresh_after_conflict(new_plan_id(), context)

    async def test_strict_validation_rejects_pending_runtime_state(self):
        plan = make_plan()
        invalid_node = plan.nodes[0].model_copy(
            update={"started_at": NOW}
        )
        invalid_plan = AgentPlan.model_validate(
            plan.model_copy(update={"nodes": [invalid_node]}).model_dump()
        )
        with self.assertRaises(PlanValidationError) as raised:
            validate_plan(invalid_plan, self.planning_config)
        self.assertEqual(raised.exception.code, "invalid_pending_node_state")
