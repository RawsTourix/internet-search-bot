import unittest

from src.agent.progress_messages import progress_text
from src.agent.protocol import ProgressEvent


class CycleCompactionProgressTests(unittest.TestCase):
    def test_progress_model_and_localizations_cover_cycle_compaction(self):
        expected = {
            "cycle_compaction_started": (
                "🧠 Освобождаю рабочий контекст…",
                "🧠 Freeing working context…",
            ),
            "cycle_compaction_done": (
                "✅ Рабочий контекст обновлён.",
                "✅ Working context updated.",
            ),
            "cycle_compaction_failed": (
                "⚠️ Не удалось безопасно сжать рабочий контекст.",
                "⚠️ Failed to compact the working context safely.",
            ),
        }

        for event_type, (ru, en) in expected.items():
            with self.subTest(event_type=event_type):
                event = ProgressEvent(
                    type=event_type,
                    message=ru,
                    data={
                        "before_tokens": 100,
                        "after_tokens": 60,
                        "generation": 2,
                        "passes_completed": 1,
                    },
                )
                self.assertEqual(event.type, event_type)
                self.assertEqual(
                    progress_text(event_type, locale_name="ru"),
                    ru,
                )
                self.assertEqual(
                    progress_text(event_type, locale_name="en"),
                    en,
                )
                serialized = event.model_dump_json()
                self.assertNotIn("raw segment", serialized)
                self.assertNotIn("summary", serialized)

    def test_context_limit_interruption_has_dedicated_progress_type(self):
        ru = progress_text(
            "context_limit_interruption",
            locale_name="ru",
        )
        en = progress_text(
            "context_limit_interruption",
            locale_name="en",
        )
        event = ProgressEvent(
            type="context_limit_interruption",
            message=en,
        )

        self.assertEqual(event.type, "context_limit_interruption")
        self.assertIn("контекст", ru)
        self.assertIn("context", en)
        self.assertNotIn("Infrastructure", en)


if __name__ == "__main__":
    unittest.main()
