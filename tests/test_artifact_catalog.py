import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from src.artifacts import (
    ArtifactConfigType,
    ArtifactProvenance,
    ArtifactPurpose,
    create_artifact_services,
)
from src.artifacts.tools import ArtifactToolController
from src.mcp.manager_context import ManagerToolContext
from src.mcp.mcp_client import SessionState
from src.runtime import ActiveAgentCycle
from src.storage import StorageConfigType, create_storage_services


class ArtifactCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        storage_config = StorageConfigType(root_dir=str(root / "storage"))
        self.storage = create_storage_services(storage_config)
        self.services = create_artifact_services(
            storage_config=storage_config,
            artifact_config=ArtifactConfigType(
                max_artifact_size_bytes=1024 * 1024,
                max_patchable_text_bytes=1024 * 1024,
                max_workspace_bytes=2 * 1024 * 1024,
            ),
            content_store=self.storage.content_store,
        )
        self.controller = ArtifactToolController(
            self.services.artifact_service,
            self.services.delivery_service,
        )
        self.cycle = ActiveAgentCycle(
            cycle_id="cycle-1",
            session_id="session-1",
            original_user_request="Find files",
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

    async def _user_artifact(self, filename: str, text: str):
        content = await self.storage.content_store.save_content(
            text,
            source_type="user_upload",
            source_name=filename,
            mime_type="text/markdown",
            encoding="utf-8",
            cycle_id="cycle-1",
        )
        _, version = await self.services.artifact_store.create_lineage(
            session_id="session-1",
            cycle_id="cycle-1",
            content_id=content.content_id,
            filename=filename,
            format_id="markdown",
            detected_mime_type="text/markdown",
            declared_mime_type="text/markdown",
            encoding="utf-8",
            provenance=ArtifactProvenance(
                origin="user_upload",
                creator="user",
                operation="ingress",
            ),
            purpose=ArtifactPurpose.INPUT,
        )
        self.cycle.artifact_refs.append(version.artifact_id)
        return version

    async def test_duplicate_user_filenames_are_ambiguous_not_resolved(self):
        first = await self._user_artifact("report.md", "first")
        second = await self._user_artifact("report.md", "second")

        outcome = await self.controller.execute(
            "artifact_list",
            {"filenames": ["report.md"]},
            self.context,
        )

        self.assertEqual(outcome.payload["type"], "artifact_catalog")
        self.assertEqual(outcome.payload["available_count"], 2)
        resolution = outcome.payload["filename_resolutions"][0]
        self.assertEqual(resolution["status"], "ambiguous")
        self.assertEqual(
            {
                item["artifact_id"] for item in resolution["candidates"]
            },
            {first.artifact_id, second.artifact_id},
        )

    async def test_catalog_filters_versions_and_pagination(self):
        created = await self.controller.execute(
            "artifact_create_text",
            {
                "filename": "notes.md",
                "text": "v1",
                "purpose": "working",
            },
            self.context,
        )
        first = created.payload["artifact"]
        replaced = await self.controller.execute(
            "artifact_replace_text",
            {
                "artifact_id": first["artifact_id"],
                "expected_current_artifact_id": first["artifact_id"],
                "new_text": "v2",
            },
            self.context,
        )
        second = replaced.payload["artifact"]

        current = await self.controller.execute(
            "artifact_list",
            {"artifact_lineage_ids": [first["artifact_lineage_id"]]},
            self.context,
        )
        self.assertEqual(current.payload["available_count"], 1)
        self.assertEqual(
            current.payload["items"][0]["artifact_id"],
            second["artifact_id"],
        )

        all_versions = await self.controller.execute(
            "artifact_list",
            {
                "artifact_lineage_ids": [first["artifact_lineage_id"]],
                "current_only": False,
            },
            self.context,
        )
        self.assertEqual(all_versions.payload["available_count"], 2)

        history = await self.controller.execute(
            "artifact_list",
            {
                "artifact_lineage_ids": [first["artifact_lineage_id"]],
                "purpose_filter": ["working"],
                "format_filter": ["markdown"],
                "include_versions": True,
                "limit": 1,
            },
            self.context,
        )
        self.assertEqual(history.payload["available_count"], 2)
        self.assertTrue(history.payload["items_truncated"])
        self.assertEqual(len(history.payload["items"]), 1)

        exact = await self.controller.execute(
            "artifact_list",
            {
                "artifact_ids": [second["artifact_id"]],
                "include_versions": True,
            },
            self.context,
        )
        self.assertEqual(exact.payload["available_count"], 1)
        self.assertEqual(
            exact.payload["items"][0]["artifact_id"],
            second["artifact_id"],
        )
        self.assertTrue(exact.payload["items"][0]["is_current"])
        self.assertEqual(exact.payload["items"][0]["versions_count"], 2)

        await self.services.artifact_store.archive_lineage(
            first["artifact_lineage_id"],
            expected_current_artifact_id=second["artifact_id"],
        )
        active_only = await self.controller.execute(
            "artifact_list",
            {"artifact_lineage_ids": [first["artifact_lineage_id"]]},
            self.context,
        )
        self.assertEqual(active_only.payload["available_count"], 0)
        archived = await self.controller.execute(
            "artifact_list",
            {
                "artifact_lineage_ids": [first["artifact_lineage_id"]],
                "include_archived": True,
            },
            self.context,
        )
        self.assertEqual(archived.payload["available_count"], 1)

    async def test_agent_filename_conflict_precedes_content_save(self):
        first = await self.controller.execute(
            "artifact_create_text",
            {"filename": "report.md", "text": "first"},
            self.context,
        )
        self.assertEqual(first.payload["type"], "artifact_created")
        original_save = self.storage.content_store.save_content
        self.storage.content_store.save_content = AsyncMock(
            wraps=original_save
        )

        conflict = await self.controller.execute(
            "artifact_create_text",
            {"filename": "folder/../report.md", "text": "second"},
            self.context,
        )

        self.assertEqual(
            conflict.payload["type"],
            "artifact_filename_conflict",
        )
        self.assertEqual(conflict.payload["filename"], "report.md")
        self.storage.content_store.save_content.assert_not_awaited()
        lineages = await self.services.artifact_store.list_lineages(
            session_id="session-1",
        )
        self.assertEqual(len(lineages), 1)


if __name__ == "__main__":
    unittest.main()
