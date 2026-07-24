import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from src.artifacts import ArtifactConfigType, create_artifact_services
from src.memory import MemoryConfigType, ResultHandling
from src.mcp.artifact_client import ArtifactMCPClient
from src.mcp.manager_context import ManagerToolContext
from src.mcp.manager_runtime_context import set_manager_context
from src.mcp.mcp_client import LLMConfigType, SessionState
from src.runtime import ActiveAgentCycle
from src.storage import StorageConfigType, create_storage_services


class ArtifactResultCompactionTests(unittest.IsolatedAsyncioTestCase):
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
                max_artifacts_per_cycle=16,
                max_concurrent_artifact_reads=2,
                max_artifact_size_bytes=1024 * 1024,
                max_patchable_text_bytes=1024 * 1024,
                max_workspace_bytes=2 * 1024 * 1024,
            ),
            content_store=storage.content_store,
        )
        self.client = ArtifactMCPClient(
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
                result_preview_max_chars=80,
            ),
        )
        self.cycle = ActiveAgentCycle(
            cycle_id="cycle-1",
            session_id="session-1",
            original_user_request="Read the reports",
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

    async def _create(self, filename: str, text: str):
        result = await self.client._call_registered_tool(
            "artifact_create_text",
            {
                "filename": filename,
                "text": text,
                "format_id": "markdown",
            },
        )
        payload = json.loads(result.content[0].text)
        return payload["artifact"]["artifact_id"], result

    async def _process(
        self,
        result,
        *,
        handling: ResultHandling,
        call_id: str,
    ):
        raw = result.content[0].text
        payload = self.client._tool_result_payload(
            "artifact_read_text",
            raw,
        )
        messages = [
            {"role": "system", "content": "system"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": call_id}],
            },
        ]
        trace = []
        outcome = await self.client._process_tool_result_for_context(
            outer_tool_name="artifact_read_text",
            effective_tool_name="artifact_read_text",
            tool_call_id=call_id,
            outer_arguments={},
            effective_arguments={},
            raw_tool_result_text=raw,
            tool_payload=payload,
            result_handling=handling,
            messages_for_llm=messages,
            original_user_request="Read the reports",
            session_id="session-1",
            cycle_id="cycle-1",
            state=self.state,
            cycle_trace=trace,
            progress_callback=None,
            request_tools=[],
            result_metadata={
                "execution_disposition": result.execution_disposition,
                "result_policy": result.result_policy,
            },
        )
        return outcome, messages, trace, raw

    async def test_small_batch_stays_inline_with_exact_item_boundary(self):
        artifact_id, _ = await self._create("small.md", "small result")
        result = await self.client._call_registered_tool(
            "artifact_read_text",
            {"artifact_ids": [artifact_id]},
        )
        self.client._call_llm_with_retries = AsyncMock()

        outcome, messages, _, _ = await self._process(
            result,
            handling=ResultHandling.AUTO,
            call_id="call-inline",
        )

        visible = json.loads(messages[-1]["content"])
        self.assertEqual(outcome.decision.representation, "inline")
        self.assertEqual(len([
            item for item in messages if item.get("role") == "tool"
        ]), 1)
        self.assertEqual(
            visible["items"][0]["requested_artifact_id"],
            artifact_id,
        )
        self.assertEqual(visible["items"][0]["text"], "small result")
        self.client._call_llm_with_retries.assert_not_awaited()

    async def test_compacted_batch_persists_exact_result_and_keeps_boundaries(self):
        first_sentinel = "FIRST_RAW_SENTINEL_" + "a" * 1_200
        second_sentinel = "SECOND_RAW_SENTINEL_" + "b" * 1_200
        first_id, _ = await self._create("first.md", first_sentinel)
        second_id, _ = await self._create("second.md", second_sentinel)
        result = await self.client._call_registered_tool(
            "artifact_read_text",
            {"artifact_ids": [second_id, first_id]},
        )
        self.client._call_llm_with_retries = AsyncMock(return_value={
            "content": json.dumps({
                "type": "result_compaction",
                "summary": "Two report files were read.",
                "key_facts": ["Two exact artifact results are stored."],
                "limitations": [],
                "suggested_follow_up": [],
                "needs_original_content": True,
            }),
        })

        outcome, messages, trace, raw = await self._process(
            result,
            handling=ResultHandling.COMPACT,
            call_id="call-compact",
        )

        visible = json.loads(messages[-1]["content"])
        self.assertEqual(outcome.decision.representation, "summarize")
        self.client._call_llm_with_retries.assert_awaited_once()
        self.assertEqual(visible["summary_scope"], "aggregate")
        self.assertEqual(visible["item_attribution"], "bounded_preview")
        self.assertEqual(
            [item["requested_artifact_id"] for item in visible["items"]],
            [second_id, first_id],
        )
        self.assertEqual(
            [item["request_index"] for item in visible["items"]],
            [0, 1],
        )
        self.assertTrue(all(
            item["representation"] == "preview"
            and not item["exact_content_available"]
            and not item["complete"]
            and item["needs_retrieval"]
            and "text" not in item
            and "preview" in item
            for item in visible["items"]
        ))
        self.assertIn("SECOND_RAW_SENTINEL_", visible["items"][0]["preview"])
        self.assertNotIn("FIRST_RAW_SENTINEL_", visible["items"][0]["preview"])
        self.assertIn("FIRST_RAW_SENTINEL_", visible["items"][1]["preview"])
        self.assertNotIn("SECOND_RAW_SENTINEL_", visible["items"][1]["preview"])
        serialized_visible = json.dumps(
            [messages, trace],
            ensure_ascii=False,
        )
        self.assertNotIn(first_sentinel, serialized_visible)
        self.assertNotIn(second_sentinel, serialized_visible)
        self.assertEqual(
            await self.client.content_store.read_text(
                outcome.content_ref.content_id
            ),
            raw,
        )

    async def test_store_only_batch_never_claims_complete_or_exact(self):
        artifact_id, _ = await self._create(
            "stored.md",
            "STORE_ONLY_SENTINEL_" + "x" * 300,
        )
        result = await self.client._call_registered_tool(
            "artifact_read_text",
            {"artifact_ids": [artifact_id]},
        )
        self.client._call_llm_with_retries = AsyncMock()

        outcome, messages, _, _ = await self._process(
            result,
            handling=ResultHandling.STORE_ONLY,
            call_id="call-store-only",
        )

        visible = json.loads(messages[-1]["content"])
        self.assertEqual(outcome.decision.representation, "store_only")
        self.assertEqual(visible["representation"], "stored_only")
        self.assertEqual(visible["summary_scope"], "aggregate")
        self.assertEqual(visible["item_attribution"], "metadata_only")
        self.assertFalse(visible["complete"])
        self.assertTrue(visible["needs_retrieval"])
        self.assertEqual(
            visible["items"][0]["representation"],
            "stored_only",
        )
        self.assertNotIn("preview", visible["items"][0])
        self.assertFalse(visible["items"][0]["exact_content_available"])
        self.assertFalse(visible["items"][0]["complete"])
        self.assertTrue(visible["items"][0]["needs_retrieval"])
        self.client._call_llm_with_retries.assert_not_awaited()

    async def test_small_receipt_bypasses_compactor(self):
        _, receipt = await self._create("receipt.md", "receipt content")
        self.client._call_llm_with_retries = AsyncMock()
        raw = receipt.content[0].text
        payload = self.client._tool_result_payload(
            "artifact_create_text",
            raw,
        )
        messages = []

        outcome = await self.client._process_tool_result_for_context(
            outer_tool_name="artifact_create_text",
            effective_tool_name="artifact_create_text",
            tool_call_id="call-receipt",
            outer_arguments={},
            effective_arguments={},
            raw_tool_result_text=raw,
            tool_payload=payload,
            result_handling=ResultHandling.COMPACT,
            messages_for_llm=messages,
            original_user_request="Create a report",
            session_id="session-1",
            cycle_id="cycle-1",
            state=self.state,
            cycle_trace=[],
            progress_callback=None,
            request_tools=[],
            result_metadata={
                "execution_disposition": receipt.execution_disposition,
                "result_policy": receipt.result_policy,
            },
        )

        self.assertEqual(outcome.decision.representation, "inline")
        self.assertEqual(
            outcome.decision.reason,
            "trusted_small_receipt_inline",
        )
        self.assertEqual(len(messages), 1)
        self.client._call_llm_with_retries.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
