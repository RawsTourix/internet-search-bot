import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from src.artifacts import (
    ArtifactConfigType,
    ArtifactDeliveryState,
    create_artifact_services,
)
from src.core.models import AgentResult, AgentStatus, ClientType
from src.memory import ResultHandling
from src.mcp.artifact_delivery_client import ArtifactDeliveryMixin
from src.mcp.artifact_delivery_runtime import (
    FinalizingArtifactDeliveryPlanningMCPClient,
)
from src.mcp.artifact_request_context import (
    set_artifact_request_cycle_identity,
)
from src.mcp.manager_context import ManagerToolContext
from src.mcp.manager_runtime_context import set_manager_context
from src.mcp.mcp_client import LLMConfigType, SessionState
from src.planning import PlanningConfigType, create_planning_services
from src.runtime import ActiveAgentCycle
from src.storage import StorageConfigType, create_storage_services


def llm_config() -> LLMConfigType:
    return LLMConfigType(
        api_url="https://example.invalid/v1/chat/completions",
        api_key="test",
        model="test-model",
        max_tokens=256,
        context_window_tokens=4096,
    )


class _ProjectionBase:
    async def process_query(self, *args, **kwargs):
        set_artifact_request_cycle_identity(("session-1", "cycle-1"))
        return AgentResult(
            content="done",
            status=AgentStatus.DONE,
            session_id="session-1",
        )


class _ProjectionHarness(ArtifactDeliveryMixin, _ProjectionBase):
    def __init__(self, artifact_services):
        self.artifact_services = artifact_services
        self.artifact_config = artifact_services.config
        self.artifact_trace_service = artifact_services.trace_service
        self._session_artifact_handoffs = {}


class ArtifactDeliveryRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.storage_config = StorageConfigType(root_dir=str(root / "storage"))
        self.storage = create_storage_services(self.storage_config)
        self.artifacts = create_artifact_services(
            storage_config=self.storage_config,
            artifact_config=ArtifactConfigType(
                max_artifact_size_bytes=1024 * 1024,
                max_patchable_text_bytes=1024 * 1024,
                max_workspace_bytes=2 * 1024 * 1024,
            ),
            content_store=self.storage.content_store,
        )
        self.planning = create_planning_services(
            storage_config=self.storage_config,
            planning_config=PlanningConfigType(),
        )
        self.client = FinalizingArtifactDeliveryPlanningMCPClient(
            llm_config(),
            storage_services=self.storage,
            artifact_services=self.artifacts,
            planning_services=self.planning,
        )
        self.cycle = ActiveAgentCycle(
            cycle_id="cycle-1",
            session_id="session-1",
            original_user_request="Create and send a report",
            messages_for_llm=[
                {"role": "user", "content": "Create and send a report"}
            ],
            cycle_trace=[],
            original_user_message_index=0,
        )
        self.state = SessionState()
        self.context = ManagerToolContext(
            session_id="session-1",
            cycle_id="cycle-1",
            active_cycle=self.cycle,
            session_state=self.state,
            client_type=ClientType.TELEGRAM,
        )
        set_manager_context(self.context)

    async def asyncTearDown(self):
        set_manager_context(None)
        await self.client.cleanup()
        self.temporary.cleanup()

    def test_production_client_keeps_planning_and_delivery_tools(self):
        expected = {
            "artifact_set_delivery",
            "artifact_create_text",
            "artifact_create_from_content",
            "agent_plan_create",
            "agent_plan_transition_node",
        }
        self.assertTrue(expected.issubset(self.client.manager_tools))
        tool = next(
            item
            for item in self.client._format_tools_for_llm()
            if item["function"]["name"] == "artifact_set_delivery"
        )
        parameters = tool["function"]["parameters"]
        schema = json.dumps(parameters)
        self.assertNotIn('"$ref"', schema)
        self.assertNotIn('"$defs"', schema)
        self.assertIn("artifact_ids", parameters["properties"])
        self.assertNotIn("artifact_id", parameters["properties"])

    async def test_delivery_tool_updates_durable_and_runtime_state(self):
        created = await self.client._call_registered_tool(
            "artifact_create_text",
            {
                "filename": "report.md",
                "text": "report body",
                "format_id": "markdown",
                "purpose": "deliverable",
            },
        )
        artifact = json.loads(created.content[0].text)["artifact"]

        selected = await self.client._call_registered_tool(
            "artifact_set_delivery",
            {
                "artifact_ids": [artifact["artifact_id"]],
                "selected": True,
            },
        )
        payload = json.loads(selected.content[0].text)

        self.assertEqual(payload["type"], "artifact_delivery_batch_selected")
        self.assertEqual(payload["items"][0]["state"], "selected")
        self.assertIsNotNone(self.cycle.artifact_state)
        self.assertEqual(len(self.cycle.artifact_state.deliveries), 1)
        stored = await self.artifacts.delivery_store.get(
            payload["items"][0]["delivery_id"]
        )
        self.assertEqual(stored.state, ArtifactDeliveryState.SELECTED)
        self.assertTrue(any(
            event.get("type") == "artifact_delivery_selected"
            for event in self.state.progress_events
        ))
        self.assertFalse(any(
            "report body" in json.dumps(event, ensure_ascii=False)
            for event in self.state.progress_events
        ))

        self.client._call_llm_with_retries = AsyncMock()
        self.client.result_compaction_service.persist_result = AsyncMock()
        raw = selected.content[0].text
        messages = []
        processing = await self.client._process_tool_result_for_context(
            outer_tool_name="artifact_set_delivery",
            effective_tool_name="artifact_set_delivery",
            tool_call_id="call-delivery-receipt",
            outer_arguments={},
            effective_arguments={},
            raw_tool_result_text=raw,
            tool_payload=self.client._tool_result_payload(
                "artifact_set_delivery",
                raw,
            ),
            result_handling=ResultHandling.COMPACT,
            messages_for_llm=messages,
            original_user_request="Create and send a report",
            session_id="session-1",
            cycle_id="cycle-1",
            state=self.state,
            cycle_trace=self.cycle.cycle_trace,
            progress_callback=None,
            request_tools=[],
            result_metadata={
                "execution_disposition": selected.execution_disposition,
                "result_policy": selected.result_policy,
            },
        )
        self.assertEqual(processing.decision.representation, "inline")
        self.assertEqual(
            processing.decision.reason,
            "trusted_small_receipt_inline",
        )
        self.client.result_compaction_service.persist_result.assert_not_awaited()
        self.client._call_llm_with_retries.assert_not_awaited()

    async def test_active_plan_without_node_blocks_delivery_selection(self):
        created = await self.client._call_registered_tool(
            "artifact_create_text",
            {
                "filename": "report.md",
                "text": "body",
                "format_id": "markdown",
            },
        )
        artifact = json.loads(created.content[0].text)["artifact"]
        self.cycle.active_plan_state = type("PlanState", (), {
            "status": "active",
            "current_node": None,
            "plan_id": "plan_" + "a" * 32,
            "revision": 1,
        })()

        blocked = await self.client._call_registered_tool(
            "artifact_set_delivery",
            {"artifact_ids": [artifact["artifact_id"]]},
        )
        payload = json.loads(blocked.content[0].text)
        self.assertEqual(payload["type"], "plan_node_required")
        refs = await self.artifacts.delivery_service.list_cycle_refs(
            session_id="session-1",
            cycle_id="cycle-1",
        )
        self.assertEqual(refs, [])

    async def test_runtime_state_recovers_reads_and_delivery_from_durable_events(self):
        created = await self.client._call_registered_tool(
            "artifact_create_text",
            {
                "filename": "recoverable.md",
                "text": "durable report body",
                "format_id": "markdown",
                "purpose": "deliverable",
            },
        )
        artifact_id = json.loads(
            created.content[0].text
        )["artifact"]["artifact_id"]
        await self.client._call_registered_tool(
            "artifact_read_text",
            {"artifact_ids": [artifact_id]},
        )
        await self.client._call_registered_tool(
            "artifact_set_delivery",
            {"artifact_ids": [artifact_id], "selected": True},
        )

        self.cycle.read_artifact_refs.clear()
        self.cycle.artifact_state = None
        await self.client._refresh_artifact_state(self.context)

        self.assertEqual(self.cycle.artifact_state.available_count, 1)
        item = self.cycle.artifact_state.items[0]
        self.assertEqual(item.artifact_id, artifact_id)
        self.assertTrue(item.read_in_current_cycle)
        self.assertTrue(item.selected_for_delivery)
        self.assertEqual(item.delivery_state, ArtifactDeliveryState.SELECTED)
        self.assertEqual(
            self.cycle.read_artifact_refs,
            [artifact_id],
        )

    async def test_agent_result_contains_refs_not_bytes(self):
        created = await self.client._call_registered_tool(
            "artifact_create_text",
            {
                "filename": "report.md",
                "text": "secret payload",
                "format_id": "markdown",
            },
        )
        artifact = json.loads(created.content[0].text)["artifact"]
        await self.artifacts.delivery_service.select(
            artifact_id=artifact["artifact_id"],
            access=self.client.artifact_tool_controller._access(self.context),
            client_type="telegram",
        )

        harness = _ProjectionHarness(self.artifacts)
        result = await harness.process_query(
            "ignored",
            client_type=ClientType.TELEGRAM,
        )

        self.assertEqual(len(result.artifacts), 1)
        serialized = json.dumps(result.artifacts)
        self.assertNotIn("secret payload", serialized)
        self.assertNotIn("content_id", serialized)
        self.assertNotIn("storage", serialized)


if __name__ == "__main__":
    unittest.main()
