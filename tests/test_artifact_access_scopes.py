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
        original_user_request="work with exact artifacts",
        messages_for_llm=[],
        cycle_trace=[],
        original_user_message_index=0,
    )


class ArtifactAccessScopeTests(unittest.IsolatedAsyncioTestCase):
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
                max_artifacts_per_cycle=8,
                max_runtime_artifact_summaries=8,
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

    def _activate(self, active_cycle: ActiveAgentCycle):
        context = self.client._activate_manager_context(
            active_cycle=active_cycle,
            state=SessionState(),
            session_id=active_cycle.session_id,
            progress_callback=None,
        )
        context.client_type = "telegram"
        return context

    async def _create(
        self,
        *,
        filename: str,
        text: str,
        purpose: str = "deliverable",
    ) -> str:
        result = await self.client._call_registered_tool(
            "artifact_create_text",
            {
                "filename": filename,
                "text": text,
                "format_id": "markdown",
                "purpose": purpose,
            },
        )
        return json.loads(result.content[0].text)["artifact"]["artifact_id"]

    async def test_session_catalog_activates_historical_exact_version(self):
        first = cycle("cycle-1", "session-1")
        self._activate(first)
        artifact_id = await self._create(filename="history.md", text="historical")

        second = cycle("cycle-2", "session-1")
        context = self._activate(second)

        current = await self.client._call_registered_tool(
            "artifact_list",
            {"scope": "current", "limit": 10},
        )
        current_payload = json.loads(current.content[0].text)
        self.assertEqual(current_payload["scope"], "current")
        self.assertEqual(current_payload["items"], [])

        session = await self.client._call_registered_tool(
            "artifact_list",
            {"scope": "session", "limit": 10},
        )
        payload = json.loads(session.content[0].text)
        self.assertEqual(payload["scope"], "session")
        self.assertEqual(payload["effective_scope"], "session")
        self.assertEqual(payload["activated_artifact_ids"], [artifact_id])
        self.assertIn(artifact_id, second.artifact_refs)
        self.assertEqual(
            second.artifact_activations[0]["reason"],
            "catalog_result",
        )
        self.assertEqual(second.artifact_activations[0]["scope"], "session")

        read = await self.client._call_registered_tool(
            "artifact_read_text",
            {"artifact_ids": [artifact_id]},
        )
        read_payload = json.loads(read.content[0].text)
        self.assertEqual(read_payload["status"], "ok")
        self.assertEqual(read_payload["items"][0]["text"], "historical")

        delivery = await self.client._call_registered_tool(
            "artifact_set_delivery",
            {"artifact_ids": [artifact_id], "selected": True},
        )
        delivery_payload = json.loads(delivery.content[0].text)
        self.assertEqual(delivery_payload["status"], "selected")

        self.assertIsNotNone(context.active_cycle.artifact_state)
        manifest_item = context.active_cycle.artifact_state.input_manifest.items[0]
        self.assertEqual(manifest_item.artifact_id, artifact_id)
        self.assertEqual(manifest_item.activation_reason, "catalog_result")
        self.assertEqual(manifest_item.activation_scope, "session")

    async def test_scope_cursor_is_bound_to_exact_scope(self):
        first = cycle("cycle-1", "session-1")
        self._activate(first)
        ids = [
            await self._create(filename="a.md", text="a"),
            await self._create(filename="b.md", text="b"),
            await self._create(filename="c.md", text="c"),
        ]

        second = cycle("cycle-2", "session-1")
        self._activate(second)
        page1 = await self.client._call_registered_tool(
            "artifact_list",
            {"scope": "session", "limit": 1},
        )
        payload1 = json.loads(page1.content[0].text)
        self.assertEqual(len(payload1["items"]), 1)
        self.assertIsNotNone(payload1["next_cursor"])

        page2 = await self.client._call_registered_tool(
            "artifact_list",
            {
                "scope": "session",
                "limit": 1,
                "cursor": payload1["next_cursor"],
            },
        )
        payload2 = json.loads(page2.content[0].text)
        self.assertEqual(len(payload2["items"]), 1)
        self.assertNotEqual(
            payload1["items"][0]["artifact_id"],
            payload2["items"][0]["artifact_id"],
        )
        self.assertTrue(set(second.artifact_refs).issubset(set(ids)))

        wrong_scope = await self.client._call_registered_tool(
            "artifact_list",
            {
                "scope": "workspace",
                "limit": 1,
                "cursor": payload1["next_cursor"],
            },
        )
        wrong_payload = json.loads(wrong_scope.content[0].text)
        self.assertEqual(wrong_payload["type"], "artifact_validation_error")
        self.assertEqual(wrong_payload["code"], "invalid_artifact_cursor")

    async def test_workspace_scope_is_explicit_filesystem_session_projection(self):
        first = cycle("cycle-1", "session-1")
        self._activate(first)
        artifact_id = await self._create(filename="workspace.md", text="workspace")

        second = cycle("cycle-2", "session-1")
        self._activate(second)
        result = await self.client._call_registered_tool(
            "artifact_list",
            {"scope": "workspace", "limit": 10},
        )
        payload = json.loads(result.content[0].text)
        self.assertEqual(payload["scope"], "workspace")
        self.assertEqual(payload["effective_scope"], "session")
        self.assertEqual(
            payload["workspace_scope_note"],
            "filesystem_v0.4_workspace_equals_session",
        )
        self.assertEqual(payload["items"][0]["artifact_id"], artifact_id)

    async def test_session_scope_does_not_cross_session_boundary(self):
        first = cycle("cycle-1", "session-1")
        self._activate(first)
        await self._create(filename="private.md", text="private")

        other = cycle("cycle-2", "session-2")
        self._activate(other)
        result = await self.client._call_registered_tool(
            "artifact_list",
            {"scope": "session", "limit": 10},
        )
        payload = json.loads(result.content[0].text)
        self.assertEqual(payload["items"], [])
        self.assertEqual(other.artifact_refs, [])
        self.assertEqual(other.artifact_activations, [])


if __name__ == "__main__":
    unittest.main()
