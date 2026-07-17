import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.mcp.mcp_client import MCPClient, SessionState


class ResultCompactionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.client = object.__new__(MCPClient)
        self.client.content_store = SimpleNamespace(
            read_content=AsyncMock(),
            read_text=AsyncMock(),
            read_range=AsyncMock(),
        )
        self.state = SessionState(tools_used=["search"])

    def _build(self, trace):
        return self.client._build_final_evidence_pack(
            original_user_request="find data",
            state=self.state,
            cycle_trace=trace,
        )

    def test_inline_evidence_preserves_full_result(self):
        result = {
            "type": "tool_result",
            "content": '{"fact":"full"}',
        }

        evidence = self._build([{
            "type": "tool_result_full",
            "tool_name": "search",
            "tool_call_id": "call-1",
            "result": result,
        }])

        self.assertEqual(evidence["tool_results"][0]["result"], result)

    def test_summarized_evidence_uses_ref_without_raw_storage_read(self):
        result_ref = {
            "type": "stored_result_ref",
            "result_id": "res_1",
            "content_id": "cnt_1",
            "tool_name": "search",
            "summary_status": "summarized",
            "summary": "summary",
            "key_facts": ["fact"],
            "limitations": ["limited"],
            "preview": None,
        }

        evidence = self._build([{
            "type": "tool_result_stored",
            "tool_name": "search",
            "tool_call_id": "call-1",
            "result_ref": result_ref,
        }])

        item = evidence["tool_results"][0]
        self.assertEqual(item["representation"], "stored_result_ref")
        self.assertEqual(item["result_ref"]["summary"], "summary")
        self.assertEqual(item["result_ref"]["key_facts"], ["fact"])
        self.assertEqual(item["result_ref"]["result_id"], "res_1")
        self.assertEqual(item["result_ref"]["content_id"], "cnt_1")
        self.assertNotIn("raw", json.dumps(evidence))
        self.client.content_store.read_content.assert_not_awaited()
        self.client.content_store.read_text.assert_not_awaited()
        self.client.content_store.read_range.assert_not_awaited()

    def test_store_only_oversized_and_failed_add_honest_limitations(self):
        traces = []
        for index, status in enumerate(
            ("store_only", "oversized", "failed"),
            start=1,
        ):
            traces.append({
                "type": "tool_result_stored",
                "tool_name": f"search-{index}",
                "tool_call_id": f"call-{index}",
                "result_ref": {
                    "type": "stored_result_ref",
                    "result_id": f"res_{index}",
                    "content_id": f"cnt_{index}",
                    "tool_name": f"search-{index}",
                    "summary_status": status,
                    "summary": None,
                    "preview": f"preview-{index}",
                },
            })

        evidence = self._build(traces)

        self.assertEqual(len(evidence["tool_results"]), 3)
        self.assertIn(
            "Полное содержимое результата не было обработано агентом.",
            evidence["tool_results"][0]["limitations"],
        )
        self.assertIn(
            "Полное содержимое результата не было обработано агентом.",
            evidence["tool_results"][1]["limitations"],
        )
        self.assertIn(
            "Оригинал сохранён, но краткое описание не было создано.",
            evidence["tool_results"][2]["limitations"],
        )
        self.assertEqual(len(evidence["limitations"]), 3)

    def test_processing_error_is_risk_evidence_with_explicit_limitation(self):
        trace = [{
            "type": "tool_result_processing_error",
            "tool_name": "search",
            "tool_call_id": "call-1",
            "error_type": "StorageError",
            "error": (
                "Результат инструмента был получен, но не сохранён "
                "и недоступен агенту."
            ),
            "result_available": False,
            "retry_recommended": False,
        }]

        evidence = self._build(trace)

        self.assertTrue(self.client._trace_has_tool_errors(trace))
        self.assertEqual(len(evidence["tool_errors"]), 1)
        self.assertEqual(
            evidence["tool_errors"][0]["type"],
            "tool_result_processing_error",
        )
        limitation = next(
            item
            for item in evidence["limitations"]
            if item["type"] == "tool_result_unavailable"
        )
        self.assertEqual(limitation["tool_name"], "search")
        self.assertEqual(limitation["tool_call_id"], "call-1")
        self.assertIn("недоступен агенту", limitation["message"])

    def test_processing_error_selects_strict_final_processing(self):
        self.client.llm_config = SimpleNamespace(final_audit=True)
        trace = [{
            "type": "tool_result_processing_error",
            "tool_name": "search",
            "tool_call_id": "call-1",
            "result_available": False,
        }]

        decision = self.client._select_final_processing_mode(
            result_text="draft",
            state=self.state,
            cycle_trace=trace,
        )

        self.assertEqual(decision.mode.value, "strict_grounded")
        self.assertEqual(decision.reason, "risky_tool_workflow")


if __name__ == "__main__":
    unittest.main()
