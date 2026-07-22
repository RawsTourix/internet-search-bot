import json
import tempfile
import unittest
from pathlib import Path

from src.artifacts import ArtifactConfigType, create_artifact_services
from src.artifacts.tools import ARTIFACT_NATIVE_TOOL_NAMES
from src.mcp.artifact_client import ArtifactMCPClient
from src.mcp.manager_context import ManagerToolContext
from src.mcp.manager_runtime_context import set_manager_context
from src.mcp.mcp_client import LLMConfigType, SessionState
from src.mcp.planning_client import PlanningMCPClient
from src.planning import PlanningConfigType, create_planning_services
from src.planning.tools import PLAN_TOOL_NAMES
from src.runtime import ActiveAgentCycle
from src.storage import StorageConfigType, create_storage_services


_MUTATIONS = {
    "artifact_create_text",
    "artifact_replace_text",
    "artifact_patch_text",
}
_READS = set(ARTIFACT_NATIVE_TOOL_NAMES) - _MUTATIONS


def llm_config():
    return LLMConfigType(
        api_url="https://example.invalid/v1/chat/completions",
        api_key="test",
        model="test-model",
        max_tokens=256,
        context_window_tokens=4096,
    )


class ArtifactRuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.storage_config = StorageConfigType(root_dir=str(root / "storage"))
        self.storage = create_storage_services(self.storage_config)
        self.artifact_services = create_artifact_services(
            storage_config=self.storage_config,
            artifact_config=ArtifactConfigType(
                max_artifact_size_bytes=1024 * 1024,
                max_patchable_text_bytes=1024 * 1024,
                max_workspace_bytes=2 * 1024 * 1024,
                max_runtime_artifact_summaries=1,
            ),
            content_store=self.storage.content_store,
        )
        self.client = ArtifactMCPClient(
            llm_config(),
            storage_services=self.storage,
            artifact_services=self.artifact_services,
        )
        self.cycle = ActiveAgentCycle(
            cycle_id="cycle-1",
            session_id="session-1",
            original_user_request="Create a report",
            messages_for_llm=[{"role": "user", "content": "Create a report"}],
            cycle_trace=[],
            original_user_message_index=0,
        )
        self.state = SessionState()
        self.context = ManagerToolContext(
            session_id="session-1",
            cycle_id="cycle-1",
            active_cycle=self.cycle,
            session_state=self.state,
        )
        set_manager_context(self.context)

    async def asyncTearDown(self):
        set_manager_context(None)
        await self.client.cleanup()
        self.temporary.cleanup()

    def test_tools_and_portable_schemas_are_registered(self):
        self.assertTrue(ARTIFACT_NATIVE_TOOL_NAMES.issubset(self.client.manager_tools))
        for item in self.client._format_tools_for_llm():
            if item["function"]["name"] not in ARTIFACT_NATIVE_TOOL_NAMES:
                continue
            serialized = json.dumps(item["function"]["parameters"])
            self.assertNotIn('"$ref"', serialized)
            self.assertNotIn('"$defs"', serialized)

    async def test_create_updates_exact_refs_runtime_state_and_progress(self):
        result = await self.client._call_registered_tool(
            "artifact_create_text",
            {
                "filename": "report.md",
                "text": "alpha beta",
                "format_id": "markdown",
                "purpose": "working",
            },
        )
        payload = json.loads(result.content[0].text)
        self.assertEqual(payload["type"], "artifact_created")
        artifact_id = payload["artifact"]["artifact_id"]
        self.assertEqual(self.cycle.artifact_refs, [artifact_id])
        self.assertIsNotNone(self.cycle.artifact_state)
        self.assertEqual(self.cycle.artifact_state.count, 1)
        self.assertEqual(
            self.cycle.artifact_state.items[0].artifact_id,
            artifact_id,
        )
        runtime_payload = self.client._iteration_runtime_payload(self.state)
        self.assertEqual(runtime_payload["artifact_state"]["count"], 1)
        self.assertTrue(any(
            event.get("type") == "artifact_created"
            for event in self.cycle.progress_events
        ))
        self.assertFalse(any(
            "alpha beta" in json.dumps(event, ensure_ascii=False)
            for event in self.cycle.progress_events
        ))

    async def test_tool_result_payload_marks_artifact_data_untrusted(self):
        parsed = self.client._tool_result_payload(
            "artifact_get",
            json.dumps({"type": "artifact_metadata", "artifact": {}}),
        )
        self.assertFalse(parsed["trusted"])
        self.assertIn("untrusted", parsed["security_note"])

    async def test_disabled_feature_omits_tools_and_controller(self):
        root = Path(self.temporary.name)
        storage_config = StorageConfigType(root_dir=str(root / "disabled-storage"))
        storage = create_storage_services(storage_config)
        services = create_artifact_services(
            storage_config=storage_config,
            artifact_config=ArtifactConfigType(enabled=False),
            content_store=storage.content_store,
        )
        client = ArtifactMCPClient(
            llm_config(),
            storage_services=storage,
            artifact_services=services,
        )
        try:
            self.assertTrue(ARTIFACT_NATIVE_TOOL_NAMES.isdisjoint(client.manager_tools))
            self.assertIsNone(client.artifact_tool_controller)
            self.assertIsNone(client.artifact_runtime)
        finally:
            await client.cleanup()

    async def test_planning_client_keeps_artifact_read_control_plane_only(self):
        root = Path(self.temporary.name)
        storage_config = StorageConfigType(root_dir=str(root / "planning-storage"))
        storage = create_storage_services(storage_config)
        artifacts = create_artifact_services(
            storage_config=storage_config,
            artifact_config=ArtifactConfigType(),
            content_store=storage.content_store,
        )
        planning = create_planning_services(
            storage_config=storage_config,
            planning_config=PlanningConfigType(),
        )
        client = PlanningMCPClient(
            llm_config(),
            storage_services=storage,
            artifact_services=artifacts,
            planning_services=planning,
        )
        try:
            self.assertTrue(ARTIFACT_NATIVE_TOOL_NAMES.issubset(client.manager_tools))
            self.assertTrue(PLAN_TOOL_NAMES.issubset(client.manager_tools))
            self.assertTrue(_READS.issubset(client.CONTROL_PLANE_MANAGER_TOOLS))
            self.assertTrue(_MUTATIONS.isdisjoint(client.CONTROL_PLANE_MANAGER_TOOLS))
        finally:
            await client.cleanup()

    async def test_active_plan_without_current_node_blocks_only_mutation(self):
        root = Path(self.temporary.name)
        storage_config = StorageConfigType(root_dir=str(root / "guard-storage"))
        storage = create_storage_services(storage_config)
        artifacts = create_artifact_services(
            storage_config=storage_config,
            artifact_config=ArtifactConfigType(),
            content_store=storage.content_store,
        )
        planning = create_planning_services(
            storage_config=storage_config,
            planning_config=PlanningConfigType(),
        )
        client = PlanningMCPClient(
            llm_config(),
            storage_services=storage,
            artifact_services=artifacts,
            planning_services=planning,
        )
        cycle = ActiveAgentCycle(
            cycle_id="cycle-guard",
            session_id="session-guard",
            original_user_request="Complex task",
            messages_for_llm=[],
            cycle_trace=[],
            original_user_message_index=0,
        )
        context = ManagerToolContext(
            session_id="session-guard",
            cycle_id="cycle-guard",
            active_cycle=cycle,
            session_state=SessionState(),
        )
        set_manager_context(context)
        try:
            await client.plan_tool_controller.execute(
                "agent_plan_create",
                {
                    "goal": "Create a report",
                    "nodes": [
                        {
                            "client_key": "create",
                            "title": "Create report",
                            "objective": "Create the report artifact",
                            "kind": "execute",
                            "depends_on": [],
                            "success_criteria": [],
                        }
                    ],
                },
                context,
            )
            blocked = await client._call_registered_tool(
                "artifact_create_text",
                {
                    "filename": "report.md",
                    "text": "content",
                    "format_id": "markdown",
                },
            )
            blocked_payload = json.loads(blocked.content[0].text)
            self.assertEqual(blocked_payload["type"], "plan_node_required")

            readable = await client._call_registered_tool(
                "artifact_list",
                {"limit": 10},
            )
            readable_payload = json.loads(readable.content[0].text)
            self.assertEqual(readable_payload["type"], "artifact_list")
        finally:
            set_manager_context(self.context)
            await client.cleanup()


if __name__ == "__main__":
    unittest.main()
