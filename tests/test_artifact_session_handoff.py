import json
import tempfile
import unittest
from pathlib import Path

from src.artifacts import ArtifactConfigType, create_artifact_services
from src.mcp.artifact_delivery_runtime import (
    FinalizingArtifactDeliveryPlanningMCPClient,
)
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


def cycle(cycle_id: str, session_id: str) -> ActiveAgentCycle:
    return ActiveAgentCycle(
        cycle_id=cycle_id,
        session_id=session_id,
        original_user_request="work with the file",
        messages_for_llm=[
            {"role": "user", "content": "work with the file"}
        ],
        cycle_trace=[],
        original_user_message_index=0,
    )


class ArtifactSessionHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        storage_config = StorageConfigType(root_dir=str(root / "storage"))
        storage = create_storage_services(storage_config)
        artifacts = create_artifact_services(
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
            llm_config(),
            storage_services=storage,
            artifact_services=artifacts,
            planning_services=planning,
        )

    async def asyncTearDown(self):
        await self.client.cleanup()
        self.temporary.cleanup()

    async def test_previous_cycle_artifact_requires_session_catalog_activation(self):
        first_cycle = cycle("cycle-1", "session-1")
        first_state = SessionState()
        self.client._activate_manager_context(
            active_cycle=first_cycle,
            state=first_state,
            session_id="session-1",
            progress_callback=None,
        )
        created = await self.client._call_registered_tool(
            "artifact_create_text",
            {
                "filename": "source.md",
                "text": "STATUS: draft",
                "format_id": "markdown",
            },
        )
        artifact_id = json.loads(created.content[0].text)["artifact"]["artifact_id"]

        session = self.client._get_or_create_session("session-1")
        self.client._append_dialog_turn(
            session,
            user_request="read source.md",
            final_answer="done",
            state=first_state,
        )

        second_cycle = cycle("cycle-2", "session-1")
        second_state = SessionState()
        self.client._activate_manager_context(
            active_cycle=second_cycle,
            state=second_state,
            session_id="session-1",
            progress_callback=None,
        )

        self.assertNotIn(artifact_id, second_cycle.artifact_refs)
        direct = await self.client._call_registered_tool(
            "artifact_read_text",
            {"artifact_ids": [artifact_id]},
        )
        direct_payload = json.loads(direct.content[0].text)
        self.assertNotEqual(direct_payload["status"], "ok")

        catalog = await self.client._call_registered_tool(
            "artifact_list",
            {"scope": "session", "limit": 10},
        )
        catalog_payload = json.loads(catalog.content[0].text)
        self.assertIn(artifact_id, catalog_payload["activated_artifact_ids"])
        self.assertIn(artifact_id, second_cycle.artifact_refs)

        result = await self.client._call_registered_tool(
            "artifact_read_text",
            {"artifact_ids": [artifact_id]},
        )
        payload = json.loads(result.content[0].text)
        self.assertEqual(payload["type"], "artifact_batch_read")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(
            payload["items"][0]["artifact"]["artifact_id"],
            artifact_id,
        )
        self.assertEqual(payload["items"][0]["text"], "STATUS: draft")

    async def test_history_does_not_cross_session_boundary(self):
        first_cycle = cycle("cycle-1", "session-1")
        first_state = SessionState()
        self.client._activate_manager_context(
            active_cycle=first_cycle,
            state=first_state,
            session_id="session-1",
            progress_callback=None,
        )
        created = await self.client._call_registered_tool(
            "artifact_create_text",
            {
                "filename": "private.md",
                "text": "private",
                "format_id": "markdown",
            },
        )
        artifact_id = json.loads(created.content[0].text)["artifact"]["artifact_id"]
        session = self.client._get_or_create_session("session-1")
        self.client._append_dialog_turn(
            session,
            user_request="create private.md",
            final_answer="done",
            state=first_state,
        )

        other_cycle = cycle("cycle-2", "session-2")
        self.client._activate_manager_context(
            active_cycle=other_cycle,
            state=SessionState(),
            session_id="session-2",
            progress_callback=None,
        )
        self.assertNotIn(artifact_id, other_cycle.artifact_refs)
        catalog = await self.client._call_registered_tool(
            "artifact_list",
            {"scope": "session", "limit": 10},
        )
        payload = json.loads(catalog.content[0].text)
        self.assertEqual(payload["items"], [])


if __name__ == "__main__":
    unittest.main()
