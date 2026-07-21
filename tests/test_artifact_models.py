import unittest
from datetime import datetime, timedelta, timezone

from pydantic import ValidationError

from src.artifacts import (
    ArtifactCandidate,
    ArtifactCandidateStatus,
    ArtifactCapability,
    ArtifactContentKind,
    ArtifactDeliveryRef,
    ArtifactDeliveryState,
    ArtifactFormatSpec,
    ArtifactLineage,
    ArtifactProvenance,
    ArtifactPurpose,
    ArtifactVersion,
    ArtifactVersionRef,
    ExactTextPatchOperation,
    is_artifact_candidate_id,
    is_artifact_delivery_id,
    is_artifact_id,
    is_artifact_lineage_id,
    new_artifact_candidate_id,
    new_artifact_delivery_id,
    new_artifact_id,
    new_artifact_lineage_id,
)
from src.storage.models import new_content_id


HASH = "sha256:" + "a" * 64
NOW = datetime.now(timezone.utc)


def provenance(**overrides):
    values = {
        "origin": "agent_created",
        "creator": "agent",
        "operation": "create_text",
    }
    values.update(overrides)
    return ArtifactProvenance(**values)


def version(**overrides):
    values = {
        "artifact_id": new_artifact_id(),
        "artifact_lineage_id": new_artifact_lineage_id(),
        "version": 1,
        "content_id": new_content_id(),
        "filename": "report.md",
        "format_id": "markdown",
        "encoding": "utf-8",
        "declared_mime_type": "text/markdown",
        "detected_mime_type": "text/markdown",
        "size_bytes": 5,
        "content_hash": HASH,
        "created_cycle_id": "cycle-1",
        "created_at": NOW,
        "provenance": provenance(),
    }
    values.update(overrides)
    return ArtifactVersion(**values)


class ArtifactModelTests(unittest.TestCase):
    def test_opaque_ids_are_distinct_and_validated(self):
        lineage_id = new_artifact_lineage_id()
        artifact_id = new_artifact_id()
        candidate_id = new_artifact_candidate_id()
        delivery_id = new_artifact_delivery_id()

        self.assertTrue(is_artifact_lineage_id(lineage_id))
        self.assertTrue(is_artifact_id(artifact_id))
        self.assertTrue(is_artifact_candidate_id(candidate_id))
        self.assertTrue(is_artifact_delivery_id(delivery_id))
        self.assertFalse(is_artifact_id(lineage_id))
        self.assertFalse(is_artifact_lineage_id(artifact_id))

    def test_lineage_requires_exact_committed_head(self):
        first = new_artifact_id()
        second = new_artifact_id()
        lineage = ArtifactLineage(
            artifact_lineage_id=new_artifact_lineage_id(),
            session_id="session-1",
            created_cycle_id="cycle-1",
            current_artifact_id=second,
            current_version=2,
            committed_artifact_ids=[first, second],
            purpose=ArtifactPurpose.WORKING,
            created_at=NOW,
            updated_at=NOW,
        )

        self.assertEqual(lineage.current_artifact_id, second)
        self.assertEqual(lineage.current_version, 2)

        with self.assertRaises(ValidationError):
            lineage.model_copy(
                update={"current_artifact_id": first},
            ).__class__.model_validate(
                lineage.model_dump() | {"current_artifact_id": first}
            )
        with self.assertRaises(ValidationError):
            ArtifactLineage(
                artifact_lineage_id=new_artifact_lineage_id(),
                session_id="session-1",
                created_cycle_id="cycle-1",
                current_artifact_id=second,
                current_version=1,
                committed_artifact_ids=[first, second],
                purpose="working",
                created_at=NOW,
                updated_at=NOW,
            )
        with self.assertRaises(ValidationError):
            ArtifactLineage(
                artifact_lineage_id=new_artifact_lineage_id(),
                session_id="session-1",
                created_cycle_id="cycle-1",
                current_artifact_id=first,
                current_version=2,
                committed_artifact_ids=[first, first],
                purpose="working",
                created_at=NOW,
                updated_at=NOW,
            )

    def test_lineage_normalizes_utc_and_rejects_time_reversal(self):
        item = ArtifactLineage(
            artifact_lineage_id=new_artifact_lineage_id(),
            session_id="session-1",
            created_cycle_id="cycle-1",
            current_artifact_id=(artifact_id := new_artifact_id()),
            current_version=1,
            committed_artifact_ids=[artifact_id],
            purpose="input",
            created_at=NOW.astimezone(timezone(timedelta(hours=3))),
            updated_at=NOW.astimezone(timezone(timedelta(hours=3))),
        )
        self.assertEqual(item.created_at.utcoffset(), timedelta(0))

        with self.assertRaises(ValidationError):
            ArtifactLineage(
                artifact_lineage_id=new_artifact_lineage_id(),
                session_id="session-1",
                created_cycle_id="cycle-1",
                current_artifact_id=(artifact_id := new_artifact_id()),
                current_version=1,
                committed_artifact_ids=[artifact_id],
                purpose="input",
                created_at=NOW,
                updated_at=NOW - timedelta(seconds=1),
            )

    def test_version_is_immutable_exact_metadata_and_sanitizes_filename(self):
        item = version(filename="../../bad\x00report.md", format_id="MARKDOWN")

        self.assertEqual(item.filename, "badreport.md")
        self.assertEqual(item.format_id, "markdown")
        self.assertNotIn("path", item.model_dump())
        self.assertNotIn("delivery_targets", item.model_dump())
        self.assertEqual(
            ArtifactVersion.model_validate_json(item.model_dump_json()),
            item,
        )

    def test_version_parent_relation_is_linear(self):
        with self.assertRaises(ValidationError):
            version(parent_artifact_id=new_artifact_id())
        with self.assertRaises(ValidationError):
            version(version=2, parent_artifact_id=None)

        parent_id = new_artifact_id()
        second = version(version=2, parent_artifact_id=parent_id)
        self.assertEqual(second.parent_artifact_id, parent_id)

    def test_provenance_is_runtime_structured_and_deduplicated(self):
        artifact_id = new_artifact_id()
        content_id = new_content_id()
        item = provenance(
            origin="tool_output",
            creator="tool",
            tool_name="document_processor",
            tool_call_id="call-1",
            source_artifact_ids=[artifact_id, artifact_id],
            source_content_ids=[content_id, content_id],
            source_message_ids=["message-1", "message-1"],
        )

        self.assertEqual(item.source_artifact_ids, [artifact_id])
        self.assertEqual(item.source_content_ids, [content_id])
        self.assertEqual(item.source_message_ids, ["message-1"])

        with self.assertRaises(ValidationError):
            provenance(creator="tool")
        with self.assertRaises(ValidationError):
            provenance(origin="tool_output")

    def test_compact_ref_is_untrusted_and_capabilities_are_deduplicated(self):
        item = ArtifactVersionRef(
            artifact_id=new_artifact_id(),
            artifact_lineage_id=new_artifact_lineage_id(),
            version=1,
            filename="report.md",
            format_id="markdown",
            mime_type="text/markdown",
            size_bytes=5,
            content_hash=HASH,
            purpose="working",
            capabilities=[
                ArtifactCapability.READ_TEXT,
                ArtifactCapability.READ_TEXT,
                ArtifactCapability.DELIVER,
            ],
        )

        self.assertFalse(item.trusted)
        self.assertEqual(
            item.capabilities,
            [ArtifactCapability.READ_TEXT, ArtifactCapability.DELIVER],
        )

    def test_candidate_promotion_state_is_consistent(self):
        base = {
            "candidate_id": new_artifact_candidate_id(),
            "session_id": "session-1",
            "cycle_id": "cycle-1",
            "content_id": new_content_id(),
            "suggested_filename": "report.docx",
            "format_id": "docx",
            "mime_type": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "size_bytes": 10,
            "content_hash": HASH,
            "source_tool_call_id": "call-1",
            "source_tool_name": "processor",
            "created_at": NOW,
        }
        available = ArtifactCandidate(**base)
        self.assertEqual(available.status, ArtifactCandidateStatus.AVAILABLE)

        promoted_values = dict(base)
        promoted_values.update(
            candidate_id=new_artifact_candidate_id(),
            status="promoted",
            promoted_artifact_id=new_artifact_id(),
        )
        promoted = ArtifactCandidate(**promoted_values)
        self.assertEqual(promoted.status, ArtifactCandidateStatus.PROMOTED)

        with self.assertRaises(ValidationError):
            ArtifactCandidate(**base, status="promoted")
        with self.assertRaises(ValidationError):
            ArtifactCandidate(
                **base,
                promoted_artifact_id=new_artifact_id(),
            )

    def test_format_spec_is_open_and_normalized(self):
        spec = ArtifactFormatSpec(
            format_id="CUSTOM.TEXT",
            canonical_mime_type="TEXT/X-CUSTOM",
            extensions=(".ctxt", "ctxt"),
            content_kind=ArtifactContentKind.NATIVE_TEXT,
            capabilities={ArtifactCapability.READ_TEXT},
            default_encoding="utf-8",
        )

        self.assertEqual(spec.format_id, "custom.text")
        self.assertEqual(spec.canonical_mime_type, "text/x-custom")
        self.assertEqual(spec.extensions, ("ctxt",))

    def test_patch_and_delivery_models_are_exact(self):
        with self.assertRaises(ValidationError):
            ExactTextPatchOperation(old_text="", new_text="value")

        patch = ExactTextPatchOperation(
            old_text="before",
            new_text="after",
            expected_occurrences=2,
        )
        self.assertEqual(patch.expected_occurrences, 2)

        delivery = ArtifactDeliveryRef(
            delivery_id=new_artifact_delivery_id(),
            artifact_id=new_artifact_id(),
            filename="../../report.md",
            format_id="markdown",
            mime_type="text/markdown",
            size_bytes=5,
            content_hash=HASH,
            client_type="telegram",
        )
        self.assertEqual(delivery.filename, "report.md")
        self.assertEqual(delivery.state, ArtifactDeliveryState.SELECTED)

    def test_extra_fields_and_invalid_hashes_are_rejected(self):
        with self.assertRaises(ValidationError):
            version(content_hash="a" * 64)
        with self.assertRaises(ValidationError):
            version(extra_value=True)
        with self.assertRaises(ValidationError):
            version(created_at=datetime.now())


if __name__ == "__main__":
    unittest.main()
