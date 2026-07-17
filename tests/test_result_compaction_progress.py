import unittest

from src.agent.progress_messages import progress_text
from src.agent.protocol import ProgressEvent
from src.mcp.mcp_client import MCPClient, SessionState


RESULT_EVENT_TYPES = (
    "result_persist_started",
    "result_persist_done",
    "result_persist_failed",
    "result_compaction_started",
    "result_compaction_done",
    "result_compaction_failed",
    "oversized_result_stored",
)


class ResultCompactionProgressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = object.__new__(MCPClient)

    def test_progress_model_and_localizations_cover_result_stages(self):
        for event_type in RESULT_EVENT_TYPES:
            with self.subTest(event_type=event_type):
                event = ProgressEvent(
                    type=event_type,
                    message=progress_text(event_type, locale_name="en"),
                )
                self.assertEqual(event.type, event_type)
                self.assertNotEqual(
                    progress_text(event_type, locale_name="ru"),
                    event_type,
                )
                self.assertNotEqual(event.message, event_type)

    async def test_result_progress_contains_ids_and_sizes_but_not_content(self):
        state = SessionState(progress_locale="ru")
        events = []
        trace = []
        data = {
            "result_id": "res_test",
            "content_id": "cnt_test",
            "size_bytes": 100,
            "size_chars": 90,
            "size_tokens_estimate": 45,
        }

        await self.client._emit_progress_event(
            state=state,
            session_id="session-1",
            cycle_id="cycle-1",
            progress_callback=events.append,
            cycle_trace=trace,
            event_type="result_persist_done",
            visibility="internal",
            data=data,
        )

        payload = events[0]
        self.assertEqual(payload["data"], data)
        self.assertNotIn("raw_result", payload["data"])
        self.assertNotIn("preview", payload["data"])
        self.assertEqual(payload["visibility"], "internal")
        self.assertEqual(trace[0]["type"], "progress_event")


if __name__ == "__main__":
    unittest.main()
