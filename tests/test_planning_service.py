import tempfile
import unittest
from pathlib import Path

from src.planning import (
    AddPlanNodeInput,
    CreatePlanNodeInput,
    PlanNodeKind,
    PlanNodeStatus,
    PlanNodeTransition,
    PlanStatus,
    PlanningConfigType,
)
from src.planning.errors import PlanAccessError, PlanValidationError
from src.planning.file_store import FileSystemPlanStore
from src.planning.service import PlanningService
from src.storage import StorageConfigType


class PlanningServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        config = PlanningConfigType()
        self.service = PlanningService(
            store=FileSystemPlanStore(
                storage_config=StorageConfigType(root_dir=str(root / "storage")),
                planning_config=config,
            ),
            config=config,
        )

    def tearDown(self):
        self.temporary.cleanup()

    async def _create_plan(self):
        return await self.service.create_plan(
            session_id="session-1",
            cycle_id="cycle-1",
            goal="Complete task",
            strategy="Collect then validate",
            nodes=[
                CreatePlanNodeInput(
                    client_key="collect",
                    title="Collect",
                    objective="Collect source data",
                    kind=PlanNodeKind.COLLECT,
                    success_criteria=["Data collected"],
                ),
                CreatePlanNodeInput(
                    client_key="validate",
                    title="Validate",
                    objective="Validate collected data",
                    kind=PlanNodeKind.VALIDATE,
                    depends_on=["collect"],
                    success_criteria=["Data validated"],
                ),
            ],
        )

    async def test_create_start_complete_and_auto_complete(self):
        plan, node_map = await self._create_plan()
        self.assertEqual(plan.revision, 1)
        self.assertEqual(
            [item.node_id for item in (await self.service.get_active_state(
                session_id="session-1",
                cycle_id="cycle-1",
                plan_id=plan.plan_id,
            )).ready_nodes],
            [node_map["collect"]],
        )

        plan = await self.service.transition_node(
            session_id="session-1",
            cycle_id="cycle-1",
            plan_id=plan.plan_id,
            expected_revision=1,
            node_id=node_map["collect"],
            transition=PlanNodeTransition.START,
            outcome_summary=None,
            reason=None,
            result_refs=[],
            artifact_refs=[],
            runtime_result_refs=[],
            runtime_artifact_refs=[],
        )
        self.assertEqual(plan.nodes[0].status, PlanNodeStatus.IN_PROGRESS)

        plan = await self.service.transition_node(
            session_id="session-1",
            cycle_id="cycle-1",
            plan_id=plan.plan_id,
            expected_revision=2,
            node_id=node_map["collect"],
            transition=PlanNodeTransition.COMPLETE,
            outcome_summary="Collected",
            reason=None,
            result_refs=[],
            artifact_refs=[],
            runtime_result_refs=[],
            runtime_artifact_refs=[],
        )
        self.assertEqual(plan.status, PlanStatus.ACTIVE)

        plan = await self.service.transition_node(
            session_id="session-1",
            cycle_id="cycle-1",
            plan_id=plan.plan_id,
            expected_revision=3,
            node_id=node_map["validate"],
            transition=PlanNodeTransition.START,
            outcome_summary=None,
            reason=None,
            result_refs=[],
            artifact_refs=[],
            runtime_result_refs=[],
            runtime_artifact_refs=[],
        )
        plan = await self.service.transition_node(
            session_id="session-1",
            cycle_id="cycle-1",
            plan_id=plan.plan_id,
            expected_revision=4,
            node_id=node_map["validate"],
            transition=PlanNodeTransition.COMPLETE,
            outcome_summary="Validated",
            reason=None,
            result_refs=[],
            artifact_refs=[],
            runtime_result_refs=[],
            runtime_artifact_refs=[],
        )
        self.assertEqual(plan.status, PlanStatus.COMPLETED)

    async def test_unknown_runtime_ref_is_rejected(self):
        plan, node_map = await self._create_plan()
        plan = await self.service.transition_node(
            session_id="session-1",
            cycle_id="cycle-1",
            plan_id=plan.plan_id,
            expected_revision=1,
            node_id=node_map["collect"],
            transition=PlanNodeTransition.START,
            outcome_summary=None,
            reason=None,
            result_refs=[],
            artifact_refs=[],
            runtime_result_refs=[],
            runtime_artifact_refs=[],
        )
        with self.assertRaises(PlanValidationError) as raised:
            await self.service.transition_node(
                session_id="session-1",
                cycle_id="cycle-1",
                plan_id=plan.plan_id,
                expected_revision=2,
                node_id=node_map["collect"],
                transition=PlanNodeTransition.COMPLETE,
                outcome_summary="Collected",
                reason=None,
                result_refs=["res_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"],
                artifact_refs=[],
                runtime_result_refs=[],
                runtime_artifact_refs=[],
            )
        self.assertEqual(raised.exception.code, "unknown_result_ref")

    async def test_block_retry_and_attempt_count(self):
        plan, node_map = await self._create_plan()
        plan = await self.service.transition_node(
            session_id="session-1",
            cycle_id="cycle-1",
            plan_id=plan.plan_id,
            expected_revision=1,
            node_id=node_map["collect"],
            transition=PlanNodeTransition.START,
            outcome_summary=None,
            reason=None,
            result_refs=[],
            artifact_refs=[],
            runtime_result_refs=[],
            runtime_artifact_refs=[],
        )
        plan = await self.service.transition_node(
            session_id="session-1",
            cycle_id="cycle-1",
            plan_id=plan.plan_id,
            expected_revision=2,
            node_id=node_map["collect"],
            transition=PlanNodeTransition.BLOCK,
            outcome_summary=None,
            reason="Need user input",
            result_refs=[],
            artifact_refs=[],
            runtime_result_refs=[],
            runtime_artifact_refs=[],
        )
        plan = await self.service.transition_node(
            session_id="session-1",
            cycle_id="cycle-1",
            plan_id=plan.plan_id,
            expected_revision=3,
            node_id=node_map["collect"],
            transition=PlanNodeTransition.RETRY,
            outcome_summary=None,
            reason=None,
            result_refs=[],
            artifact_refs=[],
            runtime_result_refs=[],
            runtime_artifact_refs=[],
        )
        self.assertEqual(plan.nodes[0].status, PlanNodeStatus.PENDING)
        self.assertEqual(plan.nodes[0].attempt_count, 1)

    async def test_add_update_remove_and_cycle_access(self):
        plan, _ = await self._create_plan()
        plan, node_map = await self.service.add_nodes(
            session_id="session-1",
            cycle_id="cycle-1",
            plan_id=plan.plan_id,
            expected_revision=1,
            nodes=[
                AddPlanNodeInput(
                    client_key="extra",
                    title="Extra",
                    objective="Additional work",
                    kind=PlanNodeKind.OTHER,
                )
            ],
        )
        extra_id = node_map["extra"]
        plan = await self.service.update_node(
            session_id="session-1",
            cycle_id="cycle-1",
            plan_id=plan.plan_id,
            expected_revision=2,
            node_id=extra_id,
            title="Updated extra",
        )
        plan = await self.service.remove_node(
            session_id="session-1",
            cycle_id="cycle-1",
            plan_id=plan.plan_id,
            expected_revision=3,
            node_id=extra_id,
        )
        self.assertNotIn(extra_id, {item.node_id for item in plan.nodes})
        with self.assertRaises(PlanAccessError):
            await self.service.get_plan(
                session_id="other",
                cycle_id="cycle-1",
                plan_id=plan.plan_id,
            )
