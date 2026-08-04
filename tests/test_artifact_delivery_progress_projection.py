import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from src.agent.progress_messages import progress_text
from src.artifacts.progress import artifact_delivery_message_projection
from src.mcp.artifact_delivery_runtime import (
    FinalizingArtifactDeliveryPlanningMCPClient,
)
from src.mcp.mcp_client import SessionState


class ArtifactDeliveryProgressProjectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = object.__new__(
            FinalizingArtifactDeliveryPlanningMCPClient
        )

    def test_select_start_message_uses_batch_count(self):
        message = self.client._tool_start_message(
            "artifact_set_delivery",
            {
                "artifact_ids": ["art_one", "art_two", "art_three"],
                "selected": True,
            },
            progress_locale="ru",
        )

        self.assertEqual(
            message,
            "📤 Выбираю файлы для отправки (3)…",
        )

    def test_cancel_start_message_uses_batch_count(self):
        message = self.client._tool_start_message(
            "artifact_set_delivery",
            {
                "artifact_ids": ["art_one", "art_two"],
                "selected": False,
            },
            progress_locale="ru",
        )

        self.assertEqual(
            message,
            "⛔ Исключаю файлы из отправки (2)…",
        )

    def test_single_file_message_remains_specific(self):
        projection = artifact_delivery_message_projection(["report.csv"])

        self.assertEqual(
            progress_text(
                "artifact_delivery_selected_one",
                locale_name="ru",
                **projection,
            ),
            "📤 Файл выбран для отправки: report.csv",
        )

    def test_filename_preview_is_bounded(self):
        projection = artifact_delivery_message_projection([
            "01-summary.md",
            "02-actions.csv",
            "03-handoff.json",
            "04-audit.md",
            "05-evidence.json",
        ])

        self.assertEqual(projection["file_count"], 5)
        self.assertEqual(projection["filenames_preview_count"], 3)
        self.assertEqual(projection["filenames_omitted_count"], 2)
        self.assertEqual(
            projection["filenames_preview"],
            "01-summary.md, 02-actions.csv, 03-handoff.json, … (+2)",
        )

    async def test_selected_event_message_matches_all_structured_items(self):
        self.client._trace_event = Mock()
        self.client._emit_progress_event = AsyncMock()
        context = SimpleNamespace(
            session_state=SessionState(progress_locale="ru"),
            session_id="telegram:conversation:1",
            cycle_id="cycle-1",
            progress_callback=None,
            active_cycle=SimpleNamespace(cycle_trace=[]),
        )
        filenames = [
            "01-order-summary.md",
            "02-customer-actions.csv",
            "03-order-handoff.json",
        ]
        outcome = SimpleNamespace(
            event_type="artifact_delivery_selected",
            severity="success",
            visibility="user",
            payload={
                "requested_count": 3,
                "selected_count": 3,
                "cancelled_count": 0,
                "items": [
                    {
                        "delivery_id": f"dlv_{index}",
                        "artifact_id": f"art_{index}",
                        "filename": filename,
                        "state": "selected",
                    }
                    for index, filename in enumerate(filenames)
                ],
            },
        )

        await self.client._record_delivery_outcome(outcome, context)

        call = self.client._emit_progress_event.await_args.kwargs
        self.assertEqual(
            call["message_key"],
            "artifact_delivery_selected_many",
        )
        self.assertEqual(call["message_kwargs"]["file_count"], 3)
        self.assertEqual(
            call["message_kwargs"]["filenames_preview"],
            ", ".join(filenames),
        )
        self.assertEqual(call["data"]["filenames"], filenames)
        self.assertEqual(call["data"]["filename_count"], 3)
        self.assertEqual(call["data"]["filenames_omitted_count"], 0)

    async def test_cancelled_event_uses_plural_projection(self):
        self.client._trace_event = Mock()
        self.client._emit_progress_event = AsyncMock()
        context = SimpleNamespace(
            session_state=SessionState(progress_locale="en"),
            session_id="web:conversation:1",
            cycle_id="cycle-2",
            progress_callback=None,
            active_cycle=SimpleNamespace(cycle_trace=[]),
        )
        outcome = SimpleNamespace(
            event_type="artifact_delivery_cancelled",
            severity="success",
            visibility="user",
            payload={
                "requested_count": 2,
                "selected_count": 0,
                "cancelled_count": 2,
                "items": [
                    {
                        "delivery_id": "dlv_1",
                        "artifact_id": "art_1",
                        "filename": "one.csv",
                        "state": "cancelled",
                    },
                    {
                        "delivery_id": "dlv_2",
                        "artifact_id": "art_2",
                        "filename": "two.json",
                        "state": "cancelled",
                    },
                ],
            },
        )

        await self.client._record_delivery_outcome(outcome, context)

        call = self.client._emit_progress_event.await_args.kwargs
        self.assertEqual(
            call["message_key"],
            "artifact_delivery_cancelled_many",
        )
        rendered = progress_text(
            call["message_key"],
            locale_name="en",
            **call["message_kwargs"],
        )
        self.assertEqual(
            rendered,
            "⛔ Files removed from delivery (2): one.csv, two.json",
        )


if __name__ == "__main__":
    unittest.main()
