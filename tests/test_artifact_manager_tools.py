import tempfile
import unittest
from pathlib import Path

from src.artifacts import ArtifactConfigType, create_artifact_services
from src.artifacts.tools import (
    ARTIFACT_NATIVE_TOOL_DEFINITIONS,
    ARTIFACT_NATIVE_TOOL_NAMES,
    ArtifactToolController,
)
from src.mcp.manager_context import ManagerToolContext
from src.mcp.mcp_client import SessionState
from src.runtime import ActiveAgentCycle
from src.storage import StorageConfigType, create_storage_services


class ArtifactManagerToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        storage_config = StorageConfigType(root_dir=str(root / "storage"))
        storage = create_storage_services(storage_config)
        artifact_config = ArtifactConfigType(
            max_artifact_size_bytes=1024 * 1024,
            max_patchable_text_bytes=1024 * 1024,
            max_workspace_bytes=2 * 1024 * 1024,
        )
        services = create_artifact_services(
            storage_config=storage_config,
            artifact_config=artifact_config,
            content_store=storage.content_store,
        )
        self.services = services
        self.controller = ArtifactToolController(
            services.artifact_service,
            services.delivery_service,
        )
        self.cycle = ActiveAgentCycle(
            cycle_id="cycle-1",
            session_id="session-1",
            original_user_request="Create a report",
            messages_for_llm=[],
            cycle_trace=[],
            original_user_message_index=0,
        )
        self.context = ManagerToolContext(
            session_id="session-1",
            cycle_id="cycle-1",
            active_cycle=self.cycle,
            session_state=SessionState(),
        )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def _create(self):
        return await self.controller.execute(
            "artifact_create_text",
            {
                "filename": "report.md",
                "text": "alpha beta",
                "format_id": "markdown",
                "purpose": "working",
            },
            self.context,
        )

    def test_definitions_match_declared_tool_names(self):
        self.assertEqual(
            {item.name for item in ARTIFACT_NATIVE_TOOL_DEFINITIONS},
            set(ARTIFACT_NATIVE_TOOL_NAMES),
        )

    async def test_create_registers_runtime_ref_and_provenance(self):
        self.cycle.active_plan_id = "plan_123"
        self.cycle.active_plan_revision = 2
        self.cycle.active_plan_node_id = "pnode_123"

        outcome = await self._create()
        self.assertEqual(outcome.payload["type"], "artifact_created")
        artifact_id = outcome.payload["artifact"]["artifact_id"]
        self.assertEqual(self.cycle.artifact_refs, [artifact_id])

        version = await self.services.artifact_store.get_version(artifact_id)
        self.assertEqual(version.provenance.plan_id, "plan_123")
        self.assertEqual(version.provenance.plan_revision, 2)
        self.assertEqual(version.provenance.plan_node_id, "pnode_123")
        self.assertEqual(version.provenance.creator, "agent")

    async def test_read_patch_and_list_use_cycle_authority(self):
        created = await self._create()
        artifact_id = created.payload["artifact"]["artifact_id"]

        read = await self.controller.execute(
            "artifact_read_text",
            {"artifact_ids": [artifact_id]},
            self.context,
        )
        self.assertEqual(read.payload["type"], "artifact_batch_read")
        self.assertEqual(read.payload["status"], "ok")
        self.assertEqual(read.payload["items"][0]["text"], "alpha beta")

        patched = await self.controller.execute(
            "artifact_patch_text",
            {
                "artifact_id": artifact_id,
                "expected_current_artifact_id": artifact_id,
                "operations": [
                    {
                        "old_text": "beta",
                        "new_text": "gamma",
                        "expected_occurrences": 1,
                    }
                ],
            },
            self.context,
        )
        self.assertEqual(patched.payload["type"], "artifact_version_created")
        next_id = patched.payload["artifact"]["artifact_id"]
        self.assertEqual(self.cycle.artifact_refs, [artifact_id, next_id])

        listed = await self.controller.execute(
            "artifact_list",
            {"limit": 10},
            self.context,
        )
        self.assertEqual(listed.payload["type"], "artifact_catalog")
        self.assertEqual(listed.payload["available_count"], 1)
        self.assertEqual(listed.payload["items"][0]["artifact_id"], next_id)
        self.assertEqual(listed.payload["items"][0]["versions_count"], 2)
        self.assertFalse(listed.payload["items"][0]["read_in_current_cycle"])
        history = await self.controller.execute(
            "artifact_list",
            {"include_versions": True},
            self.context,
        )
        first = next(
            item
            for item in history.payload["items"]
            if item["artifact_id"] == artifact_id
        )
        self.assertTrue(first["read_in_current_cycle"])

    async def test_stale_mutation_returns_current_exact_ref(self):
        created = await self._create()
        first_id = created.payload["artifact"]["artifact_id"]
        second = await self.controller.execute(
            "artifact_replace_text",
            {
                "artifact_id": first_id,
                "expected_current_artifact_id": first_id,
                "new_text": "second",
            },
            self.context,
        )
        second_id = second.payload["artifact"]["artifact_id"]

        stale = await self.controller.execute(
            "artifact_replace_text",
            {
                "artifact_id": first_id,
                "expected_current_artifact_id": first_id,
                "new_text": "stale",
            },
            self.context,
        )
        self.assertEqual(stale.payload["type"], "artifact_version_conflict")
        self.assertTrue(stale.payload["retryable"])
        self.assertEqual(stale.payload["current_artifact_id"], second_id)
        self.assertEqual(
            stale.payload["current_artifact"]["artifact_id"],
            second_id,
        )

    async def test_invalid_arguments_and_access_are_structured(self):
        invalid = await self.controller.execute(
            "artifact_read_text",
            {"artifact_ids": ["not-an-id"], "extra": True},
            self.context,
        )
        self.assertEqual(invalid.payload["code"], "invalid_tool_arguments")

        partial = await self.controller.execute(
            "artifact_read_text",
            {"artifact_ids": ["art_" + "0" * 32]},
            self.context,
        )
        self.assertEqual(partial.payload["status"], "rejected")
        self.assertIn(
            partial.payload["items"][0]["code"],
            {"artifact_not_found", "artifact_access_error"},
        )

    def test_llm_schemas_use_only_new_manager_contract(self):
        definitions = {
            item.name: item for item in ARTIFACT_NATIVE_TOOL_DEFINITIONS
        }
        self.assertNotIn("artifact_get", ARTIFACT_NATIVE_TOOL_NAMES)
        read_schema = definitions["artifact_read_text"].parameters()
        search_schema = definitions["artifact_search_text"].parameters()
        self.assertIn("artifact_ids", read_schema["properties"])
        self.assertNotIn("artifact_id", read_schema["properties"])
        self.assertNotIn("offset_chars", read_schema["properties"])
        self.assertNotIn("limit_chars", read_schema["properties"])
        self.assertIn("artifact_ids", search_schema["properties"])
        self.assertNotIn("artifact_id", search_schema["properties"])
        self.assertNotIn("limit", search_schema["properties"])

    async def test_cycle_capacity_blocks_mutation_before_persistence(self):
        self.services.config.max_artifacts_per_cycle = 1
        first = await self._create()
        self.assertEqual(first.payload["type"], "artifact_created")
        second = await self._create()
        self.assertEqual(second.payload["type"], "artifact_limit_error")
        lineages = await self.services.artifact_store.list_lineages(
            session_id="session-1"
        )
        self.assertEqual(len(lineages), 1)


if __name__ == "__main__":
    unittest.main()
