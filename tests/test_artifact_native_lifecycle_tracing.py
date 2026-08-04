import json
import tempfile
import unittest
from pathlib import Path

from src.artifacts import ArtifactConfigType, create_artifact_services
from src.core.models import ClientType
from src.mcp.artifact_delivery_runtime import (
    FinalizingArtifactDeliveryPlanningMCPClient,
)
from src.mcp.manager_context import ManagerToolContext
from src.mcp.manager_runtime_context import set_manager_context
from src.mcp.mcp_client import LLMConfigType, SessionState
from src.planning import PlanningConfigType, create_planning_services
from src.runtime import ActiveAgentCycle
from src.storage import StorageConfigType, create_storage_services


class ArtifactNativeLifecycleTracingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        storage_config = StorageConfigType(root_dir=str(root / "storage"))
        storage = create_storage_services(storage_config)
        self.artifacts = create_artifact_services(
            storage_config=storage_config,
            artifact_config=ArtifactConfigType(
                max_artifact_size_bytes=1024 * 1024,
                max_patchable_text_bytes=1024 * 1024,
                max_workspace_bytes=2 * 1024 * 1024,
            ),
            content_store=storage.content_store,
        )
        planning = create_planning_services(
            storage_config=storage_config,
            planning_config=PlanningConfigType(),
        )
        self.client = FinalizingArtifactDeliveryPlanningMCPClient(
            LLMConfigType(
                api_url="https://example.invalid/v1/chat/completions",
                api_key="test",
                model="test-model",
                max_tokens=256,
                context_window_tokens=4096,
            ),
            storage_services=storage,
            artifact_services=self.artifacts,
            planning_services=planning,
        )
        self.cycle = ActiveAgentCycle(
            cycle_id="cycle-native-trace",
            session_id="session-native-trace",
            original_user_request="create and read a file",
            messages_for_llm=[
                {"role": "user", "content": "create and read a file"}
            ],
            cycle_trace=[],
            original_user_message_index=0,
        )
        self.state = SessionState()
        self.context = ManagerToolContext(
            session_id="session-native-trace",
            cycle_id="cycle-native-trace",
            active_cycle=self.cycle,
            session_state=self.state,
            client_type=ClientType.TELEGRAM,
        )
        set_manager_context(self.context)

    async def asyncTearDown(self):
        set_manager_context(None)
        await self.client.cleanup()
        self.temporary.cleanup()

    async def test_create_read_and_search_emit_safe_session_events(self):
        secret_text = "private body marker 3c09018e"
        created = await self.client._call_registered_tool(
            "artifact_create_text",
            {
                "filename": "native-trace.md",
                "text": secret_text,
                "format_id": "markdown",
                "purpose": "working",
            },
        )
        artifact = json.loads(created.content[0].text)["artifact"]

        await self.client._call_registered_tool(
            "artifact_read_text",
            {"artifact_ids": [artifact["artifact_id"]]},
        )
        await self.client._call_registered_tool(
            "artifact_search_text",
            {
                "artifact_ids": [artifact["artifact_id"]],
                "query": "private",
            },
        )

        events = await self.artifacts.trace_service.list_session(
            "session-native-trace"
        )
        event_types = [item.event_type for item in events]
        self.assertIn("artifact_created", event_types)
        self.assertIn("artifact_read_completed", event_types)
        self.assertIn("artifact_search_completed", event_types)

        created_event = next(
            item for item in events if item.event_type == "artifact_created"
        )
        self.assertEqual(created_event.artifact.artifact_id, artifact["artifact_id"])
        self.assertEqual(created_event.artifact.filename, "native-trace.md")
        read_event = next(
            item for item in events
            if item.event_type == "artifact_read_completed"
        )
        self.assertEqual(
            read_event.data["artifact_ids"],
            [artifact["artifact_id"]],
        )

        session_dir = self.artifacts.trace_store._session_dir(
            "session-native-trace"
        )
        raw = "\n".join(
            path.read_text(encoding="utf-8")
            for path in session_dir.glob("*.jsonl")
        )
        self.assertNotIn(secret_text, raw)
        self.assertNotIn('"text"', raw)
        self.assertNotIn('"query":"private"', raw)


if __name__ == "__main__":
    unittest.main()
