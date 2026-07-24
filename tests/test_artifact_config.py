import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from src.artifacts import (
    ArtifactConfigType,
    ArtifactConfigValidationError,
    load_artifact_config,
)


class ArtifactConfigTests(unittest.TestCase):
    def test_defaults_are_bounded_and_consistent(self):
        config = ArtifactConfigType()

        self.assertTrue(config.enabled)
        self.assertEqual(config.max_artifacts_per_cycle, 32)
        self.assertGreaterEqual(
            config.max_read_chars,
            config.max_inline_text_chars,
        )
        self.assertGreaterEqual(
            config.max_artifact_size_bytes,
            config.max_patchable_text_bytes,
        )
        self.assertGreaterEqual(
            config.max_workspace_bytes,
            config.max_artifact_size_bytes,
        )
        self.assertFalse(config.auto_select_deliverables)
        self.assertEqual(config.max_concurrent_artifact_reads, 4)
        self.assertEqual(
            config.max_composite_result_bytes,
            8 * 1024 * 1024,
        )

    def test_unknown_and_invalid_values_are_rejected(self):
        with self.assertRaises(ValidationError):
            ArtifactConfigType(unknown=True)
        with self.assertRaises(ValidationError):
            ArtifactConfigType(max_artifacts_per_cycle=0)
        with self.assertRaises(ValidationError):
            ArtifactConfigType(
                max_artifacts_per_cycle=2,
                max_concurrent_artifact_reads=3,
            )
        with self.assertRaises(ValidationError):
            ArtifactConfigType(max_composite_result_bytes=0)
        with self.assertRaises(ValidationError):
            ArtifactConfigType(
                max_inline_text_chars=101,
                max_read_chars=100,
            )
        with self.assertRaises(ValidationError):
            ArtifactConfigType(
                max_artifact_size_bytes=100,
                max_patchable_text_bytes=101,
            )
        with self.assertRaises(ValidationError):
            ArtifactConfigType(
                max_artifact_size_bytes=101,
                max_patchable_text_bytes=100,
                max_workspace_bytes=100,
            )

    def test_loader_uses_optional_section_and_custom_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"
            path.write_text(json.dumps({"servers": []}), encoding="utf-8")
            self.assertEqual(load_artifact_config(str(path)), ArtifactConfigType())

            path.write_text(
                json.dumps(
                    {
                        "artifacts": {
                            "enabled": False,
                            "max_artifacts_per_cycle": 8,
                            "max_artifact_size_bytes": 1_000,
                            "max_patchable_text_bytes": 500,
                            "max_workspace_bytes": 2_000,
                        }
                    }
                ),
                encoding="utf-8",
            )
            config = load_artifact_config(str(path))
            self.assertFalse(config.enabled)
            self.assertEqual(config.max_artifacts_per_cycle, 8)

    def test_loader_wraps_invalid_root_section_and_io(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.json"

            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(ArtifactConfigValidationError):
                load_artifact_config(str(path))

            path.write_text(
                json.dumps({"artifacts": []}),
                encoding="utf-8",
            )
            with self.assertRaises(ArtifactConfigValidationError):
                load_artifact_config(str(path))

            path.write_text(
                json.dumps({"artifacts": {"unknown": True}}),
                encoding="utf-8",
            )
            with self.assertRaises(ArtifactConfigValidationError):
                load_artifact_config(str(path))

            with self.assertRaises(ArtifactConfigValidationError):
                load_artifact_config(str(path.with_name("missing.json")))


if __name__ == "__main__":
    unittest.main()
