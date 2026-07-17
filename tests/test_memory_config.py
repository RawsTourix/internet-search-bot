import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from src.memory import (
    MemoryConfigType,
    MemoryConfigValidationError,
)
from src.mcp.mcp_client import load_config


class MemoryConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def _write_config(self, memory_marker=...):
        payload = {
            "servers": [
                {
                    "name": "test",
                    "connect_type": "executable",
                    "executable": "python",
                    "enabled": True,
                }
            ],
            "llm": {
                "api_url": "https://example.invalid/v1/chat/completions",
            },
        }
        if memory_marker is not ...:
            payload["memory"] = memory_marker
        path = self.root / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_config_without_memory_uses_defaults_and_returns_four_values(self):
        loaded = load_config(str(self._write_config()))

        self.assertEqual(len(loaded), 4)
        self.assertEqual(loaded[3], MemoryConfigType())

    def test_full_memory_config_is_loaded(self):
        _, _, _, memory = load_config(
            str(
                self._write_config({
                    "enable_result_compaction": False,
                    "inline_result_max_input_ratio": 0.2,
                    "single_pass_summary_max_input_ratio": 0.8,
                    "result_summary_target_ratio": 0.05,
                    "result_preview_max_chars": 123,
                })
            )
        )

        self.assertFalse(memory.enable_result_compaction)
        self.assertEqual(memory.inline_result_max_input_ratio, 0.2)
        self.assertEqual(memory.single_pass_summary_max_input_ratio, 0.8)
        self.assertEqual(memory.result_summary_target_ratio, 0.05)
        self.assertEqual(memory.result_preview_max_chars, 123)

    def test_invalid_memory_config_is_managed(self):
        invalid_values = (
            {"inline_result_max_input_ratio": 0},
            {"inline_result_max_input_ratio": 1.1},
            {
                "inline_result_max_input_ratio": 0.6,
                "single_pass_summary_max_input_ratio": 0.6,
            },
            {
                "single_pass_summary_max_input_ratio": 0.6,
                "result_summary_target_ratio": 0.6,
            },
            {"result_preview_max_chars": 0},
            {"extra": True},
        )

        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(MemoryConfigValidationError):
                    load_config(str(self._write_config(value)))

    def test_direct_model_validation_rejects_invalid_values(self):
        for kwargs in (
            {"single_pass_summary_max_input_ratio": 0},
            {"result_summary_target_ratio": 2},
            {"result_preview_max_chars": -1},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValidationError):
                    MemoryConfigType(**kwargs)


if __name__ == "__main__":
    unittest.main()
