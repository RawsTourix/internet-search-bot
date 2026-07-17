import hashlib
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from src.memory import (
    MemoryConfigType,
    ResultCompactionService,
    ResultCompactionSummary,
    ResultContextBudgetPolicy,
    ResultHandling,
)
from src.storage.models import ContentRef, new_content_id, new_result_id


class FakeContentStore:
    def __init__(self):
        self.calls = []
        self.saved = {}

    async def save_content(self, content, **kwargs):
        self.calls.append((content, kwargs))
        encoded = content.encode("utf-8")
        ref = ContentRef(
            content_id=new_content_id(),
            source_type=kwargs["source_type"],
            source_name=kwargs.get("source_name"),
            mime_type=kwargs.get("mime_type") or "text/plain",
            size_bytes=len(encoded),
            size_chars=len(content),
            size_tokens_estimate=kwargs.get("size_tokens_estimate"),
            content_hash=(
                "sha256:" + hashlib.sha256(encoded).hexdigest()
            ),
            created_at=datetime.now(timezone.utc),
            metadata=kwargs.get("metadata") or {},
        )
        self.saved[ref.content_id] = content
        return ref


def make_policy():
    return ResultContextBudgetPolicy(
        context_window_tokens=10_000,
        reserved_output_tokens=1_000,
        max_output_tokens=500,
        context_safety_ratio=0.8,
        context_compaction_target_ratio=0.5,
        inline_result_max_input_ratio=0.1,
        single_pass_summary_max_input_ratio=0.6,
        result_summary_target_ratio=0.01,
        max_in_memory_content_bytes=100_000,
    )


class ResultCompactionServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = FakeContentStore()
        self.service = ResultCompactionService(
            content_store=self.store,
            config=MemoryConfigType(result_preview_max_chars=40),
            budget_policy=make_policy(),
        )

    async def test_persistence_uses_effective_tool_and_safe_metadata(self):
        result_id = new_result_id()
        ref = await self.service.persist_result(
            result_id=result_id,
            raw_result='{"items":[1,2]}',
            effective_tool_name="web_search",
            manager_tool_name="mcp_call_tool",
            cycle_id="cycle-1",
            tool_call_id="call-1",
            result_handling=ResultHandling.COMPACT,
            result_tokens=12,
        )

        raw, kwargs = self.store.calls[0]
        self.assertEqual(raw, '{"items":[1,2]}')
        self.assertEqual(kwargs["source_type"], "tool_result")
        self.assertEqual(kwargs["source_name"], "web_search")
        self.assertEqual(kwargs["mime_type"], "application/json")
        self.assertEqual(kwargs["encoding"], "utf-8")
        self.assertEqual(kwargs["cycle_id"], "cycle-1")
        self.assertEqual(kwargs["tool_call_id"], "call-1")
        self.assertEqual(kwargs["metadata"], {
            "result_id": result_id,
            "manager_tool_name": "mcp_call_tool",
            "result_handling": "compact",
        })
        self.assertNotIn("path", ref.model_dump())

    async def test_plain_text_mime_type(self):
        await self.service.persist_result(
            result_id=new_result_id(),
            raw_result="plain text",
            effective_tool_name="tool",
            manager_tool_name="tool",
            cycle_id="cycle-1",
            tool_call_id="call-1",
            result_handling=ResultHandling.STORE_ONLY,
            result_tokens=5,
        )

        self.assertEqual(self.store.calls[0][1]["mime_type"], "text/plain")

    def test_small_mime_detection_validates_json(self):
        self.assertEqual(
            self.service._detect_mime_type('{"items":[1,2]}'),
            "application/json",
        )
        self.assertEqual(
            self.service._detect_mime_type("{not valid json}"),
            "text/plain",
        )
        self.assertEqual(
            self.service._detect_mime_type('"json scalar"'),
            "text/plain",
        )

    def test_large_mime_detection_does_not_parse_full_json(self):
        padding = "x" * (
            self.service.JSON_MIME_PARSE_MAX_CHARS + 1
        )

        with patch(
            "src.memory.result_compaction.json.loads",
            side_effect=AssertionError("large payload must not be parsed"),
        ):
            self.assertEqual(
                self.service._detect_mime_type(" \n{" + padding),
                "application/json",
            )
            self.assertEqual(
                self.service._detect_mime_type("plain " + padding),
                "text/plain",
            )

    def test_preview_is_bounded_and_unicode_safe(self):
        short = "Привет"
        long = "данные🙂" * 20

        self.assertEqual(self.service.build_preview(short), short)
        preview = self.service.build_preview(long)
        self.assertLessEqual(len(preview), 40)
        self.assertTrue(preview.endswith("…[preview truncated]"))
        preview.encode("utf-8")

    async def test_summarized_ref_contains_summary_sizes_and_hash(self):
        raw = "content"
        content_ref = await self.service.persist_result(
            result_id=new_result_id(),
            raw_result=raw,
            effective_tool_name="search",
            manager_tool_name="search",
            cycle_id="cycle-1",
            tool_call_id="call-1",
            result_handling=ResultHandling.COMPACT,
            result_tokens=4,
        )
        summary = ResultCompactionSummary(
            summary="  Main result  ",
            key_facts=[" one ", "", "one", "two"],
            limitations=[" none "],
        )
        result_id = new_result_id()

        ref = self.service.build_summarized_ref(
            result_id=result_id,
            content_ref=content_ref,
            cycle_id="cycle-1",
            tool_call_id="call-1",
            tool_name="search",
            summary=summary,
            size_chars=len(raw),
            size_tokens_estimate=4,
        )

        self.assertEqual(ref.result_id, result_id)
        self.assertEqual(ref.content_id, content_ref.content_id)
        self.assertEqual(ref.summary, "Main result")
        self.assertEqual(ref.key_facts, ["one", "two"])
        self.assertEqual(ref.limitations, ["none"])
        self.assertEqual(ref.size_bytes, len(raw.encode()))
        self.assertEqual(ref.size_chars, len(raw))
        self.assertEqual(ref.content_hash, content_ref.content_hash)
        self.assertTrue(ref.summary_generated_by_llm)
        self.assertFalse(ref.trusted)

    async def test_store_only_oversized_and_failed_refs_are_honest(self):
        raw = "x" * 100
        result_id = new_result_id()
        content_ref = await self.service.persist_result(
            result_id=result_id,
            raw_result=raw,
            effective_tool_name="search",
            manager_tool_name="search",
            cycle_id="cycle-1",
            tool_call_id="call-1",
            result_handling=ResultHandling.STORE_ONLY,
            result_tokens=50,
        )
        builders = (
            ("store_only", self.service.build_store_only_ref),
            ("oversized", self.service.build_oversized_ref),
            ("failed", self.service.build_failed_ref),
        )

        for status, builder in builders:
            with self.subTest(status=status):
                ref = builder(
                    result_id=result_id,
                    content_ref=content_ref,
                    cycle_id="cycle-1",
                    tool_call_id="call-1",
                    tool_name="search",
                    raw_result=raw,
                    size_tokens_estimate=50,
                )
                self.assertEqual(ref.summary_status, status)
                self.assertIsNone(ref.summary)
                self.assertTrue(ref.preview)
                self.assertTrue(ref.needs_retrieval)
                self.assertFalse(ref.summary_generated_by_llm)


if __name__ == "__main__":
    unittest.main()
