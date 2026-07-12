import json
import unittest
from datetime import datetime, timezone

from pydantic import ValidationError

from src.storage.models import (
    ArtifactRef,
    ContentMetadata,
    ContentRef,
    StoredResultRef,
    new_artifact_id,
    new_content_id,
    new_result_id,
)


HASH = "sha256:" + "a" * 64


def make_content_ref(**overrides):
    values = {
        "content_id": new_content_id(),
        "source_type": "tool_result",
        "mime_type": "text/plain",
        "size_bytes": 5,
        "size_chars": 5,
        "content_hash": HASH,
        "created_at": datetime.now(timezone.utc),
    }
    values.update(overrides)
    return ContentRef(**values)


class StorageModelTests(unittest.TestCase):
    def test_content_ref_json_round_trip_and_utc_timestamp(self):
        original = make_content_ref(metadata={"language": "ru"})

        restored = ContentRef.model_validate_json(original.model_dump_json())

        self.assertEqual(restored, original)
        self.assertEqual(restored.created_at.utcoffset().total_seconds(), 0)
        self.assertNotIn("path", restored.model_dump())

    def test_content_metadata_preserves_schema_version(self):
        ref = make_content_ref()
        metadata = ContentMetadata(
            **ref.model_dump(),
            encoding="utf-8",
            cycle_id="cycle-1",
        )

        payload = json.loads(metadata.model_dump_json())

        self.assertEqual(payload["schema_version"], 1)

    def test_stored_result_accepts_all_summary_statuses(self):
        for status in ("inline", "summarized", "store_only", "oversized", "failed"):
            with self.subTest(status=status):
                result = StoredResultRef(
                    result_id=new_result_id(),
                    content_id=new_content_id(),
                    cycle_id="cycle-1",
                    tool_call_id="call-1",
                    tool_name="search",
                    summary_status=status,
                )
                self.assertEqual(result.summary_status, status)
                self.assertEqual(
                    StoredResultRef.model_validate_json(result.model_dump_json()),
                    result,
                )

    def test_stored_result_rejects_unknown_summary_status(self):
        with self.assertRaises(ValidationError):
            StoredResultRef(
                result_id=new_result_id(),
                content_id=new_content_id(),
                cycle_id="cycle-1",
                tool_call_id="call-1",
                tool_name="search",
                summary_status="unknown",
            )

    def test_invalid_version_id_hash_extra_and_naive_datetime_are_rejected(self):
        with self.assertRaises(ValidationError):
            self._artifact(version=0)
        with self.assertRaises(ValidationError):
            make_content_ref(content_id="../../escape")
        with self.assertRaises(ValidationError):
            make_content_ref(content_hash="a" * 64)
        with self.assertRaises(ValidationError):
            make_content_ref(extra_value=True)
        with self.assertRaises(ValidationError):
            make_content_ref(created_at=datetime.now())

    def test_filename_is_sanitized_and_delivery_targets_are_deduplicated(self):
        artifact = self._artifact(
            filename="../../bad\x00\x01report.md",
            delivery_targets=["telegram", "telegram", "web"],
        )

        self.assertEqual(artifact.filename, "badreport.md")
        self.assertEqual(artifact.delivery_targets, ["telegram", "web"])

    def test_collection_defaults_are_not_shared(self):
        content_one = make_content_ref()
        content_two = make_content_ref()
        content_one.metadata["changed"] = True

        artifact_one = self._artifact()
        artifact_two = self._artifact()
        artifact_one.delivery_targets.append("telegram")

        self.assertEqual(content_two.metadata, {})
        self.assertEqual(artifact_two.delivery_targets, [])

    @staticmethod
    def _artifact(**overrides):
        values = {
            "artifact_id": new_artifact_id(),
            "cycle_id": "cycle-1",
            "filename": "report.md",
            "mime_type": "text/markdown",
            "size_bytes": 5,
            "content_hash": HASH,
            "source": "agent_generated",
            "created_at": datetime.now(timezone.utc),
        }
        values.update(overrides)
        return ArtifactRef(**values)
