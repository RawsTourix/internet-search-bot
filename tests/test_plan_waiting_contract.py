import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.core.models import AgentResult, AgentStatus
from src.mcp.mcp_client import LLMConfigType
from src.mcp.planning_client import PlanningMCPClient
from src.mcp.planning_runtime import FinalizingPlanningMCPClient
from src.planning import PlanningConfigType, create_planning_services
from src.runtime import ActiveAgentCycle
from src.storage import StorageConfigType, create_storage_services


class PlanWaitingContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        storage_config = StorageConfigType(root_dir=str(root / "storage"))
        self.storage_services = create_storage_services(storage_config)
        self.planning_services = create_planning_services(
            storage_config=storage_config,
            planning_config=PlanningConfigType(),
        )
        self.client = FinalizingPlanningMCPClient(
            LLMConfigType(
                api_url="https://example.invalid/v1/chat/completions",
                api_key="test",
                model="test-model",
                max_tokens=256,
                context_window_tokens=4096,
            ),
            storage_services=self.storage_services,
            planning_services=self.planning_services,
        )

    async def asyncTearDown(self):
        await self.client.cleanup()
        self.temporary.cleanup()

    async def test_waiting_result_is_resumable_when_pending_cycle_is_saved(self):
        session_id = "session-waiting"
        session = self.client._get_or_create_session(session_id)
        session.pending_cycle = ActiveAgentCycle(
            cycle_id="cycle-waiting",
            session_id=session_id,
            original_user_request="Need approval",
            messages_for_llm=[],
            cycle_trace=[],
            original_user_message_index=0,
            status="waiting_user",
            waiting_question="Approve?",
        )
        base_result = AgentResult(
            content="Approve?",
            status=AgentStatus.WAITING_USER,
            session_id=session_id,
            can_resume=False,
        )

        with patch.object(
            PlanningMCPClient,
            "process_query",
            new=AsyncMock(return_value=base_result),
        ):
            result = await self.client.process_query(
                "request",
                session_id=session_id,
            )

        self.assertTrue(result.can_resume)

    async def test_waiting_result_is_not_marked_resumable_without_saved_cycle(self):
        session_id = "session-without-pending-cycle"
        base_result = AgentResult(
            content="Question",
            status=AgentStatus.WAITING_USER,
            session_id=session_id,
            can_resume=False,
        )

        with patch.object(
            PlanningMCPClient,
            "process_query",
            new=AsyncMock(return_value=base_result),
        ):
            result = await self.client.process_query(
                "request",
                session_id=session_id,
            )

        self.assertFalse(result.can_resume)

    async def test_manager_tool_schema_is_available_through_schema_lookup(self):
        payload = await self.client._manager_get_tool_schema(
            {"tool_name": "agent_plan_create"}
        )

        self.assertEqual(payload["type"], "mcp_tool_schema")
        self.assertEqual(payload["tool"]["name"], "agent_plan_create")
        self.assertEqual(payload["tool"]["source"], "manager")
        self.assertIn("properties", payload["tool"]["inputSchema"])
