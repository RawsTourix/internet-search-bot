import tempfile
import unittest
from pathlib import Path

from src.artifacts import (
    ArtifactCandidate,
    ArtifactCandidateStatus,
    ArtifactConfigType,
    create_artifact_services,
    new_artifact_candidate_id,
    utc_now,
)
from src.mcp.artifact_delivery_runtime import (
    FinalizingArtifactDeliveryPlanningMCPClient,
)
from src.mcp.manager_context import ManagerToolContext
from src.mcp.mcp_client import LLMConfigType, SessionState
from src.planning import PlanningConfigType, create_planning_services
from src.runtime import ActiveAgentCycle
from src.storage import StorageConfigType, create_storage_services


class ArtifactCandidateRecoveryTests(unittest.IsolatedAsyncioTestCase):
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
            LLMConfigType(
                api_url="https://example.invalid/v1/chat/completions",
                api_key="test",
                model="test-model",
                max_tokens=256,
                context_window_tokens=4096,
            ),
            storage_services=self.storage,
            artifact_services=self.artifacts,
            planning_services=self.planning,
        )

    async def asyncTearDown(self):
        await self.client.cleanup()
        self.temporary.cleanup()

    async def _candidate(self, *, filename: str):
        content = await self.storage.content_store.save_content(
            b"processor output",
            source_type="artifact_candidate",
            source_name=filename,
            mime_type="text/markdown",
            cycle_id="cycle-1",
            tool_call_id="tool-1",
            metadata={"artifact_format_id": "markdown"},
        )
        return await self.artifacts.candidate_store.create(
            ArtifactCandidate(
                candidate_id=new_artifact_candidate_id(),
                session_id="session-1",
                cycle_id="cycle-1",
                content_id=content.content_id,
                suggested_filename=filename,
                format_id="markdown",
                mime_type="text/markdown",
                size_bytes=content.size_bytes,
                content_hash=content.content_hash,
                source_tool_call_id="tool-1",
                source_tool_name="processor",
                status=ArtifactCandidateStatus.AVAILABLE,
                created_at=utc_now(),
            )
        )

    async def test_available_candidates_are_restored_from_authoritative_store(self):
        available = await self._candidate(filename="available.md")
        promoted = await self._candidate(filename="promoted.md")
        artifact = await self.artifacts.promotion_service.create_artifact(
            candidate_id=promoted.candidate_id,
            allowed_candidate_ids=[promoted.candidate_id],
            session_id="session-1",
            cycle_id="cycle-1",
            purpose="working",
        )

        cycle = ActiveAgentCycle(
            cycle_id="cycle-1",
            session_id="session-1",
            original_user_request="Resume",
            messages_for_llm=[{"role": "user", "content": "Resume"}],
            cycle_trace=[],
            original_user_message_index=0,
            artifact_refs=[artifact.artifact_id],
            artifact_candidate_refs=[],
        )
        context = ManagerToolContext(
            session_id="session-1",
            cycle_id="cycle-1",
            active_cycle=cycle,
            session_state=SessionState(),
        )

        await self.client._refresh_artifact_state(context)

        self.assertEqual(
            cycle.artifact_candidate_refs,
            [available.candidate_id],
        )
        self.assertNotIn(promoted.candidate_id, cycle.artifact_candidate_refs)


if __name__ == "__main__":
    unittest.main()
