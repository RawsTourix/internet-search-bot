import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.artifacts import (
    ArtifactCandidate,
    ArtifactCandidateStatus,
    ArtifactConfigType,
    create_artifact_services,
    new_artifact_candidate_id,
    utc_now,
)
from src.artifacts.candidate_tools import ARTIFACT_CANDIDATE_TOOL_NAMES
from src.mcp.artifact_client import ArtifactMCPClient
from src.mcp.manager_context import ManagerToolContext
from src.mcp.manager_runtime_context import set_manager_context
from src.mcp.mcp_client import LLMConfigType, SessionState
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


class ArtifactCandidateToolTests(unittest.IsolatedAsyncioTestCase):
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
                max_artifacts_per_cycle=8,
            ),
            content_store=self.storage.content_store,
        )
        self.client = ArtifactMCPClient(
            llm_config(),
            storage_services=self.storage,
            artifact_services=self.artifacts,
        )
        self.cycle = ActiveAgentCycle(
            cycle_id="cycle-1",
            session_id="session-1",
            original_user_request="Process the file",
            messages_for_llm=[{"role": "user", "content": "Process the file"}],
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

    async def _candidate(
        self,
        payload: bytes = b"processed text",
        *,
        filename: str = "result.md",
        format_id: str = "markdown",
        mime_type: str = "text/markdown",
        session_id: str = "session-1",
        cycle_id: str = "cycle-1",
    ) -> ArtifactCandidate:
        content = await self.storage.content_store.save_content(
            payload,
            source_type="artifact_candidate",
            source_name=filename,
            mime_type=mime_type,
            cycle_id=cycle_id,
            tool_call_id="tool-call-1",
            metadata={"artifact_format_id": format_id},
        )
        return await self.artifacts.candidate_store.create(
            ArtifactCandidate(
                candidate_id=new_artifact_candidate_id(),
                session_id=session_id,
                cycle_id=cycle_id,
                content_id=content.content_id,
                suggested_filename=filename,
                format_id=format_id,
                mime_type=mime_type,
                size_bytes=content.size_bytes,
                content_hash=content.content_hash,
                source_tool_call_id="tool-call-1",
                source_tool_name="document_processor",
                status=ArtifactCandidateStatus.AVAILABLE,
                created_at=utc_now(),
            )
        )

    def test_tools_and_portable_schemas_are_registered(self):
        self.assertTrue(ARTIFACT_CANDIDATE_TOOL_NAMES.issubset(self.client.manager_tools))
        self.assertIn(
            "artifact_candidate_list",
            self.client.CONTROL_PLANE_MANAGER_TOOLS,
        )
        self.assertNotIn(
            "artifact_create_from_content",
            self.client.CONTROL_PLANE_MANAGER_TOOLS,
        )
        for item in self.client._format_tools_for_llm():
            if item["function"]["name"] not in ARTIFACT_CANDIDATE_TOOL_NAMES:
                continue
            serialized = json.dumps(item["function"]["parameters"])
            self.assertNotIn('"$ref"', serialized)
            self.assertNotIn('"$defs"', serialized)
            self.assertNotIn("content_id", serialized)

    async def test_list_filters_by_runtime_authority(self):
        allowed = await self._candidate()
        hidden = await self._candidate(filename="hidden.md")
        foreign = await self._candidate(
            filename="foreign.md",
            session_id="session-2",
            cycle_id="cycle-2",
        )
        self.cycle.artifact_candidate_refs = [allowed.candidate_id]

        result = await self.client._call_registered_tool(
            "artifact_candidate_list",
            {"offset": 0, "limit": 10},
        )
        payload = json.loads(result.content[0].text)

        self.assertEqual(payload["type"], "artifact_candidate_list")
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["items"][0]["candidate_id"], allowed.candidate_id)
        ids = {item["candidate_id"] for item in payload["items"]}
        self.assertNotIn(hidden.candidate_id, ids)
        self.assertNotIn(foreign.candidate_id, ids)

    async def test_promote_candidate_updates_cycle_and_runtime_state(self):
        candidate = await self._candidate()
        self.cycle.artifact_candidate_refs = [candidate.candidate_id]

        result = await self.client._call_registered_tool(
            "artifact_create_from_content",
            {
                "candidate_id": candidate.candidate_id,
                "purpose": "working",
                "title": "Processed report",
            },
        )
        payload = json.loads(result.content[0].text)
        artifact_id = payload["artifact"]["artifact_id"]

        self.assertEqual(payload["type"], "artifact_created")
        self.assertEqual(payload["source_candidate_id"], candidate.candidate_id)
        self.assertEqual(self.cycle.artifact_refs, [artifact_id])
        self.assertEqual(self.cycle.artifact_candidate_refs, [])
        self.assertIsNotNone(self.cycle.artifact_state)
        self.assertEqual(self.cycle.artifact_state.items[0].artifact_id, artifact_id)
        promoted = await self.artifacts.candidate_store.get(candidate.candidate_id)
        self.assertEqual(promoted.status, ArtifactCandidateStatus.PROMOTED)
        self.assertEqual(promoted.promoted_artifact_id, artifact_id)
        self.assertTrue(any(
            event.get("type") == "artifact_created"
            for event in self.state.progress_events
        ))

    async def test_repeated_promotion_is_idempotent(self):
        candidate = await self._candidate()
        self.cycle.artifact_candidate_refs = [candidate.candidate_id]

        first = await self.client._call_registered_tool(
            "artifact_create_from_content",
            {"candidate_id": candidate.candidate_id},
        )
        first_payload = json.loads(first.content[0].text)
        artifact_id = first_payload["artifact"]["artifact_id"]

        self.cycle.artifact_candidate_refs = [candidate.candidate_id]
        second = await self.client._call_registered_tool(
            "artifact_create_from_content",
            {"candidate_id": candidate.candidate_id},
        )
        second_payload = json.loads(second.content[0].text)

        self.assertEqual(second_payload["artifact"]["artifact_id"], artifact_id)
        lineages = await self.artifacts.artifact_store.list_lineages(
            session_id="session-1",
            include_archived=True,
        )
        self.assertEqual(len(lineages), 1)

    async def test_create_version_from_candidate_and_stale_head_conflict(self):
        original_result = await self.client._call_registered_tool(
            "artifact_create_text",
            {
                "filename": "report.md",
                "text": "v1",
                "format_id": "markdown",
            },
        )
        original = json.loads(original_result.content[0].text)["artifact"]
        candidate = await self._candidate(payload=b"v2")
        self.cycle.artifact_candidate_refs = [candidate.candidate_id]

        promoted_result = await self.client._call_registered_tool(
            "artifact_create_version_from_content",
            {
                "candidate_id": candidate.candidate_id,
                "artifact_lineage_id": original["artifact_lineage_id"],
                "expected_current_artifact_id": original["artifact_id"],
            },
        )
        promoted = json.loads(promoted_result.content[0].text)
        self.assertEqual(promoted["type"], "artifact_version_created")
        self.assertEqual(promoted["artifact"]["version"], 2)

        another = await self._candidate(payload=b"v3", filename="result-v3.md")
        self.cycle.artifact_candidate_refs = [another.candidate_id]
        stale_result = await self.client._call_registered_tool(
            "artifact_create_version_from_content",
            {
                "candidate_id": another.candidate_id,
                "artifact_lineage_id": original["artifact_lineage_id"],
                "expected_current_artifact_id": original["artifact_id"],
            },
        )
        stale = json.loads(stale_result.content[0].text)
        self.assertEqual(stale["type"], "artifact_version_conflict")
        self.assertEqual(
            stale["current_artifact_id"],
            promoted["artifact"]["artifact_id"],
        )

    async def test_active_plan_without_node_blocks_promotion_but_not_listing(self):
        candidate = await self._candidate()
        self.cycle.artifact_candidate_refs = [candidate.candidate_id]
        self.cycle.active_plan_state = SimpleNamespace(
            status="active",
            current_node=None,
            plan_id="plan_" + "a" * 32,
            revision=1,
        )

        listed = await self.client._call_registered_tool(
            "artifact_candidate_list",
            {"limit": 10},
        )
        self.assertEqual(
            json.loads(listed.content[0].text)["type"],
            "artifact_candidate_list",
        )

        blocked = await self.client._call_registered_tool(
            "artifact_create_from_content",
            {"candidate_id": candidate.candidate_id},
        )
        payload = json.loads(blocked.content[0].text)
        self.assertEqual(payload["type"], "plan_node_required")
        self.assertEqual(self.cycle.artifact_refs, [])
        self.assertEqual(
            self.cycle.artifact_candidate_refs,
            [candidate.candidate_id],
        )


if __name__ == "__main__":
    unittest.main()
