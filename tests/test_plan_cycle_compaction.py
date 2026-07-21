import tempfile
import unittest
from pathlib import Path

from src.mcp.manager_context import ManagerToolContext
from src.mcp.mcp_client import SessionState
from src.memory import (
    CycleCompactionResult,
    CycleSegmentSelection,
    CycleWorkingState,
)
from src.planning import new_plan_id, new_plan_node_id
from src.planning.cycle_memory import PlanningCycleCompactionService
from src.planning.runtime_context import (
    PlanningAwareContentStore,
    set_manager_context,
)
from src.runtime import ActiveAgentCycle
from src.storage import StorageConfigType, create_storage_services


class PlanCycleCompactionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        base_services = create_storage_services(
            StorageConfigType(root_dir=str(root / "storage"))
        )
        self.store = PlanningAwareContentStore(base_services.content_store)
        self.plan_id = new_plan_id()
        self.node_id = new_plan_node_id()
        self.cycle = ActiveAgentCycle(
            cycle_id="cycle-1",
            session_id="session-1",
            original_user_request="Complex task",
            messages_for_llm=[],
            cycle_trace=[],
            original_user_message_index=0,
            active_plan_id=self.plan_id,
            active_plan_revision=7,
            active_plan_node_id=self.node_id,
        )
        self.context = ManagerToolContext(
            session_id="session-1",
            cycle_id="cycle-1",
            active_cycle=self.cycle,
            session_state=SessionState(),
        )
        set_manager_context(self.context)

    async def asyncTearDown(self):
        set_manager_context(None)
        self.temporary.cleanup()

    async def test_content_metadata_receives_plan_association(self):
        ref = await self.store.save_content(
            "payload",
            source_type="tool_result",
            mime_type="text/plain",
            encoding="utf-8",
            cycle_id="cycle-1",
        )
        metadata = await self.store.get_metadata(ref.content_id)
        self.assertEqual(metadata.metadata["plan_id"], self.plan_id)
        self.assertEqual(metadata.metadata["plan_revision"], 7)
        self.assertEqual(metadata.metadata["plan_node_id"], self.node_id)

    async def test_compactor_cannot_replace_runtime_plan_identity(self):
        content_ref = await self.store.save_content(
            "segment",
            source_type="cycle_segment",
            mime_type="application/json",
            encoding="utf-8",
            cycle_id="cycle-1",
        )
        selection = CycleSegmentSelection(
            start=1,
            end_exclusive=2,
            messages=[{"role": "assistant", "content": "old"}],
            estimated_tokens=20,
            selected_block_count=1,
            eligible_block_count=1,
            reason="test",
        )
        fake_plan_id = new_plan_id()
        fake_node_id = new_plan_node_id()
        result = CycleCompactionResult(
            summary="Compacted",
            working_state=CycleWorkingState(
                current_goal="Continue",
                active_plan_id=fake_plan_id,
                active_plan_revision=99,
                active_plan_node_id=fake_node_id,
            ),
        )
        service = PlanningCycleCompactionService(content_store=self.store)
        memory = service.build_working_memory(
            active_cycle=self.cycle,
            selection=selection,
            segment_content_ref=content_ref,
            compaction_result=result,
        )
        self.assertEqual(memory.working_state.active_plan_id, self.plan_id)
        self.assertEqual(memory.working_state.active_plan_revision, 7)
        self.assertEqual(memory.working_state.active_plan_node_id, self.node_id)
