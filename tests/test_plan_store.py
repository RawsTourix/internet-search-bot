import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.planning import (
    AgentPlan,
    PlanNode,
    PlanNodeKind,
    PlanStatus,
    PlanningConfigType,
    new_plan_id,
    new_plan_node_id,
)
from src.planning.errors import (
    PlanNotFoundError,
    PlanRevisionConflictError,
    PlanValidationError,
)
from src.planning.file_store import FileSystemPlanStore, _serialize_model
from src.storage import StorageConfigType


NOW = datetime.now(timezone.utc)


def make_plan(*, cycle_id="cycle-1", revision=1):
    node = PlanNode(
        node_id=new_plan_node_id(),
        key="root",
        title="Root",
        objective="Complete root",
        kind=PlanNodeKind.PROCESS,
        created_at=NOW,
        updated_at=NOW,
    )
    return AgentPlan(
        plan_id=new_plan_id(),
        session_id="session-1",
        cycle_id=cycle_id,
        goal="Complete task",
        status=PlanStatus.ACTIVE,
        revision=revision,
        nodes=[node],
        created_at=NOW,
        updated_at=NOW,
    )


class PlanStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = FileSystemPlanStore(
            storage_config=StorageConfigType(root_dir=str(self.root / "storage")),
            planning_config=PlanningConfigType(),
        )

    def tearDown(self):
        self.temporary.cleanup()

    async def test_create_get_and_revision_history(self):
        plan = make_plan()
        created = await self.store.create_plan(plan)
        self.assertEqual(created, plan)
        current = await self.store.get_plan(plan.plan_id)
        self.assertEqual(current.revision, 1)

        revision_two = AgentPlan.model_validate(
            plan.model_copy(
                deep=True,
                update={"revision": 2, "goal": "Updated goal"},
            ).model_dump()
        )
        await self.store.save_revision(revision_two, expected_revision=1)
        self.assertEqual((await self.store.get_plan(plan.plan_id)).revision, 2)
        self.assertEqual(
            (await self.store.get_plan(plan.plan_id, revision=1)).goal,
            "Complete task",
        )

    async def test_revision_conflict(self):
        plan = make_plan()
        await self.store.create_plan(plan)
        candidate = AgentPlan.model_validate(
            plan.model_copy(update={"revision": 2}).model_dump()
        )
        with self.assertRaises(PlanRevisionConflictError):
            await self.store.save_revision(candidate, expected_revision=0)

    async def test_orphan_revision_is_not_exposed_before_metadata_commit(self):
        plan = make_plan()
        await self.store.create_plan(plan)
        orphan = AgentPlan.model_validate(
            plan.model_copy(update={"revision": 2, "goal": "Orphan"}).model_dump()
        )
        revision_path = (
            self.store.root
            / plan.plan_id
            / "revisions"
            / "000002.json"
        )
        revision_path.write_bytes(_serialize_model(orphan))
        with self.assertRaises(PlanNotFoundError):
            await self.store.get_plan(plan.plan_id, revision=2)
        self.assertEqual((await self.store.get_plan(plan.plan_id)).revision, 1)

    async def test_list_cycle_plans(self):
        first = make_plan(cycle_id="cycle-a")
        second = make_plan(cycle_id="cycle-b")
        await self.store.create_plan(first)
        await self.store.create_plan(second)
        refs = await self.store.list_cycle_plans("cycle-a")
        self.assertEqual([item.plan_id for item in refs], [first.plan_id])

    async def test_invalid_plan_id_does_not_escape_root(self):
        with self.assertRaises(PlanValidationError):
            await self.store.get_plan("../../etc/passwd")

    async def test_parallel_distinct_plan_creates(self):
        plans = [make_plan(cycle_id=f"cycle-{index}") for index in range(5)]
        await asyncio.gather(*(self.store.create_plan(plan) for plan in plans))
        loaded = await asyncio.gather(
            *(self.store.get_plan(plan.plan_id) for plan in plans)
        )
        self.assertEqual({item.plan_id for item in loaded}, {item.plan_id for item in plans})
