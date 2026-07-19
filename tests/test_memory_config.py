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

    def _write_config(self, memory_marker=..., *, llm_marker=None):
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
        if llm_marker:
            payload["llm"].update(llm_marker)
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
                    "result_summary_target_tokens": 300,
                    "result_compaction_max_output_tokens": 1200,
                    "result_preview_max_chars": 123,
                    "cycle_compaction_summary_target_tokens": 600,
                    "cycle_compaction_max_output_tokens": 2400,
                    "cycle_compaction_keep_recent_blocks": 4,
                    "cycle_compaction_max_passes": 5,
                })
            )
        )

        self.assertFalse(memory.enable_result_compaction)
        self.assertEqual(memory.inline_result_max_input_ratio, 0.2)
        self.assertEqual(memory.single_pass_summary_max_input_ratio, 0.8)
        self.assertEqual(memory.result_summary_target_tokens, 300)
        self.assertEqual(memory.result_compaction_max_output_tokens, 1200)
        self.assertEqual(memory.result_preview_max_chars, 123)
        self.assertEqual(
            memory.cycle_compaction_summary_target_tokens,
            600,
        )
        self.assertEqual(
            memory.cycle_compaction_max_output_tokens,
            2400,
        )
        self.assertEqual(memory.cycle_compaction_keep_recent_blocks, 4)
        self.assertEqual(memory.cycle_compaction_max_passes, 5)

    def test_tokenizer_encoding_is_loaded_with_llm_config(self):
        _, llm, _, _ = load_config(
            str(
                self._write_config(
                    llm_marker={
                        "model": "openai/gpt-oss-120b",
                        "tokenizer_encoding": "o200k_harmony",
                    }
                )
            )
        )

        self.assertEqual(llm.tokenizer_encoding, "o200k_harmony")

    def test_invalid_memory_config_is_managed(self):
        invalid_values = (
            {"inline_result_max_input_ratio": 0},
            {"inline_result_max_input_ratio": 1.1},
            {
                "inline_result_max_input_ratio": 0.6,
                "single_pass_summary_max_input_ratio": 0.6,
            },
            {
                "result_summary_target_tokens": 1000,
                "result_compaction_max_output_tokens": 1000,
            },
            {"result_preview_max_chars": 0},
            {"result_compaction_max_output_tokens": 0},
            {"cycle_compaction_summary_target_tokens": 0},
            {
                "cycle_compaction_summary_target_tokens": 2000,
                "cycle_compaction_max_output_tokens": 2000,
            },
            {"cycle_compaction_keep_recent_blocks": 0},
            {"cycle_compaction_max_passes": 0},
            {"cycle_compaction_max_passes": 11},
            {"result_summary_target_ratio": 0.01},
            {"cycle_compaction_summary_target_ratio": 0.02},
            {"extra": True},
        )

        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(MemoryConfigValidationError):
                    load_config(str(self._write_config(value)))

    def test_direct_model_validation_rejects_invalid_values(self):
        for kwargs in (
            {"single_pass_summary_max_input_ratio": 0},
            {"result_summary_target_tokens": 0},
            {"result_compaction_max_output_tokens": -1},
            {"result_preview_max_chars": -1},
            {"cycle_compaction_max_output_tokens": 0},
            {"cycle_compaction_keep_recent_blocks": -1},
            {"cycle_compaction_max_passes": 11},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValidationError):
                    MemoryConfigType(**kwargs)


if __name__ == "__main__":
    unittest.main()
