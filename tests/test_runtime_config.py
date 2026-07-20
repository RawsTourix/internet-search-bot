import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from src.mcp.mcp_client import load_config
from src.runtime import (
    RuntimeConfigType,
    RuntimeConfigValidationError,
)


class RuntimeConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def _write_config(self, runtime_marker=...):
        payload = {
            "servers": [{
                "name": "test",
                "connect_type": "executable",
                "executable": "python",
                "enabled": True,
            }],
            "llm": {
                "api_url": "https://example.invalid/v1/chat/completions",
            },
        }
        if runtime_marker is not ...:
            payload["runtime"] = runtime_marker
        path = self.root / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_config_without_runtime_uses_defaults(self):
        loaded = load_config(str(self._write_config()))

        self.assertEqual(len(loaded), 5)
        self.assertEqual(loaded[4], RuntimeConfigType())

    def test_full_runtime_config_is_loaded(self):
        _, _, _, _, runtime = load_config(
            str(self._write_config({
                "mcp_startup_timeout": 20,
                "mcp_transport_call_timeout": 12,
                "mcp_reconnect_timeout": 8,
                "mcp_runtime_close_timeout": 6,
                "mcp_call_retries_after_recovery": 2,
            }))
        )

        self.assertEqual(runtime.mcp_startup_timeout, 20)
        self.assertEqual(runtime.mcp_transport_call_timeout, 12)
        self.assertEqual(runtime.mcp_reconnect_timeout, 8)
        self.assertEqual(runtime.mcp_runtime_close_timeout, 6)
        self.assertEqual(runtime.mcp_call_retries_after_recovery, 2)

    def test_invalid_runtime_config_is_managed(self):
        invalid_values = (
            {"mcp_startup_timeout": 0},
            {"mcp_transport_call_timeout": -1},
            {"mcp_reconnect_timeout": float("inf")},
            {"mcp_runtime_close_timeout": 0},
            {"mcp_call_retries_after_recovery": -1},
            {"mcp_call_retries_after_recovery": 6},
            {"extra": True},
        )

        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(RuntimeConfigValidationError):
                    load_config(str(self._write_config(value)))

    def test_direct_model_validation_rejects_invalid_values(self):
        for kwargs in (
            {"mcp_startup_timeout": 0},
            {"mcp_runtime_close_timeout": float("nan")},
            {"mcp_call_retries_after_recovery": 6},
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValidationError):
                    RuntimeConfigType(**kwargs)


if __name__ == "__main__":
    unittest.main()
