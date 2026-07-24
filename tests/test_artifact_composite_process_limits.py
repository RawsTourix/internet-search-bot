import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from src.artifacts import ArtifactConfigType, create_artifact_services
from src.memory import MemoryConfigType, ResultHandling
from src.mcp.artifact_client import ArtifactMCPClient
from src.mcp.artifact_composite_compaction import ArtifactCompositeCompactionMixin
from src.mcp.artifact_composite_preview import ArtifactCompositePreviewMixin
from src.mcp.artifact_delivery_runtime import (
    FinalizingArtifactDeliveryPlanningMCPClient,
)
from src.mcp.manager_context import ManagerToolContext
from src.mcp.manager_runtime_context import set_manager_context
from src.mcp.mcp_client import LLMConfigType, SessionState
from src.runtime import ActiveAgentCycle
from src.storage import StorageConfigType, create_storage_services


class _PreviewArtifactMCPClient(
    ArtifactCompositePreviewMixin,
    ArtifactCompositeCompactionMixin,
    ArtifactMCPClient,
):
    pass


class ArtifactCompositeProcessLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        storage_config = StorageConfigType(
            root_dir=str(root / "storage"),
            max_in_memory_content_bytes=100_000,
        )
        storage = create_storage_services(storage_config)
        artifacts = create_artifact_services(
            storage_config=storage_config,
            artifact_config=ArtifactConfigType(
                max_artifacts_per_cycle=8,
                max_concurrent_artifact_reads=2,
                max_composite_result_bytes=300,
                max_artifact_size_bytes=1024 * 1024,
                max_patchable_text_bytes=1024 * 1024,
                max_workspace_bytes=2 * 1024 * 1024,
            ),
            content_store=storage.content_store,
        )
        self.client = _PreviewArtifactMCPClient(
            LLMConfigType(
                api_url="https://example.invalid/v1/chat/completions",
                api_key="test",
                model="test-model",
                context_window_tokens=40_000,
                reserved_output_tokens=1_000,
                max_tokens=500,
                context_safety_ratio=0.8,
                context_compaction_target_ratio=0.5,
            ),
            storage_services=storage,
            artifact_services=artifacts,
            memory_config=MemoryConfigType(
                inline_result_max_input_ratio=0.04,
                single_pass_summary_max_input_ratio=0.30,
                result_summary_target_tokens=128,
                result_compaction_max_output_tokens=500,
                result_preview_max_chars=60,
            ),
        )
        self.cycle = ActiveAgentCycle(
            cycle_id="cycle-1",
            session_id="session-1",
            original_user_request="Read the report",
            messages_for_llm=[],
            cycle_trace=[],
            original_user_message_index=0,
        )
        self.state = SessionState(progress_locale="en")
        set_manager_context(ManagerToolContext(
            session_id="session-1",
            cycle_id="cycle-1",
            active_cycle=self.cycle,
            session_state=self.state,
        ))

    async def asyncTearDown(self):
        set_manager_context(None)
        await self.client.cleanup()
        self.temporary.cleanup()

    async def test_production_runtime_wires_both_composite_mixins(self):
        self.assertTrue(issubclass(
            FinalizingArtifactDeliveryPlanningMCPClient,
            ArtifactCompositePreviewMixin,
        ))
        self.assertTrue(issubclass(
            FinalizingArtifactDeliveryPlanningMCPClient,
            ArtifactCompositeCompactionMixin,
        ))

    async def test_process_limited_read_keeps_bounded_exact_preview(self):
        source_text = "PROCESS_LIMIT_SENTINEL_" + "z" * 2_000
        created = await self.client._call_registered_tool(
            "artifact_create_text",
            {
                "filename": "large.md",
                "text": source_text,
                "format_id": "markdown",
            },
        )
        artifact_id = json.loads(created.content[0].text)["artifact"]["artifact_id"]

        result = await self.client._call_registered_tool(
            "artifact_read_text",
            {"artifact_ids": [artifact_id]},
        )
        raw = result.content[0].text
        payload = json.loads(raw)
        item = payload["items"][0]
        self.assertEqual(item["representation"], "stored_only")
        self.assertEqual(item["text"], "")
        self.assertIn("preview", item)
        self.assertLessEqual(len(item["preview"]), 60)
        self.assertIn("PROCESS_LIMIT_SENTINEL_", item["preview"])
        self.assertNotEqual(item["preview"], source_text)

        self.client._call_llm_with_retries = AsyncMock()
        messages = [
            {"role": "system", "content": "system"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call-store-only"}],
            },
        ]
        tool_payload = self.client._tool_result_payload(
            "artifact_read_text",
            raw,
        )
        outcome = await self.client._process_tool_result_for_context(
            outer_tool_name="artifact_read_text",
            effective_tool_name="artifact_read_text",
            tool_call_id="call-store-only",
            outer_arguments={},
            effective_arguments={},
            raw_tool_result_text=raw,
            tool_payload=tool_payload,
            result_handling=ResultHandling.STORE_ONLY,
            messages_for_llm=messages,
            original_user_request="Read the report",
            session_id="session-1",
            cycle_id="cycle-1",
            state=self.state,
            cycle_trace=[],
            progress_callback=None,
            request_tools=[],
            result_metadata={
                "execution_disposition": result.execution_disposition,
                "result_policy": result.result_policy,
            },
        )

        visible = json.loads(messages[-1]["content"])
        self.assertEqual(outcome.decision.representation, "store_only")
        self.assertEqual(visible["item_attribution"], "bounded_preview")
        self.assertEqual(visible["items"][0]["representation"], "preview")
        self.assertIn(
            "PROCESS_LIMIT_SENTINEL_",
            visible["items"][0]["preview"],
        )
        self.client._call_llm_with_retries.assert_not_awaited()
