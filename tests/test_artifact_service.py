import tempfile
import unittest
from pathlib import Path

from src.artifacts import (
    ArtifactAccessContext,
    ArtifactAccessError,
    ArtifactCapabilityError,
    ArtifactConfigType,
    ArtifactProvenance,
    ArtifactPurpose,
    ArtifactValidationError,
    ArtifactVersionConflictError,
    ExactTextPatchOperation,
    create_artifact_services,
)
from src.storage import StorageConfigType, create_storage_services


def provenance(operation: str, *, edit: bool = False) -> ArtifactProvenance:
    return ArtifactProvenance(
        origin="agent_edit" if edit else "agent_created",
        creator="agent",
        operation=operation,
    )


class ArtifactServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.storage_config = StorageConfigType(root_dir=str(root / "storage"))
        self.storage = create_storage_services(self.storage_config)
        self.artifact_config = ArtifactConfigType(
            max_artifact_size_bytes=1024 * 1024,
            max_patchable_text_bytes=1024 * 1024,
            max_workspace_bytes=2 * 1024 * 1024,
            max_read_chars=1000,
            max_inline_text_chars=100,
        )
        self.artifacts = create_artifact_services(
            storage_config=self.storage_config,
            artifact_config=self.artifact_config,
            content_store=self.storage.content_store,
        )
        self.service = self.artifacts.artifact_service

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def _create_markdown(self, text="alpha beta gamma"):
        return await self.service.create_text(
            session_id="session-1",
            cycle_id="cycle-1",
            filename="report.md",
            text=text,
            format_id="markdown",
            provenance=provenance("create"),
            purpose=ArtifactPurpose.WORKING,
        )

    @staticmethod
    def access(*artifact_ids, session_id="session-1"):
        return ArtifactAccessContext(
            session_id=session_id,
            cycle_id="cycle-1",
            allowed_artifact_ids=list(artifact_ids),
        )

    async def test_create_read_search_replace_and_patch(self):
        first = await self._create_markdown("alpha beta gamma")
        access = self.access(first.artifact_id)

        fragment = await self.service.read_text(
            first.artifact_id,
            access=access,
            offset_chars=6,
            limit_chars=4,
        )
        self.assertEqual(fragment.text, "beta")
        self.assertEqual(fragment.total_chars, 16)
        self.assertFalse(fragment.eof)

        matches = await self.service.search_text(
            first.artifact_id,
            access=access,
            query="gamma",
        )
        self.assertEqual(len(matches.matches), 1)

        second = await self.service.replace_text(
            artifact_id=first.artifact_id,
            expected_current_artifact_id=first.artifact_id,
            access=access,
            cycle_id="cycle-1",
            new_text="alpha beta delta",
            provenance=provenance("replace", edit=True),
        )
        self.assertEqual(second.version, 2)
        self.assertEqual(second.artifact_lineage_id, first.artifact_lineage_id)

        third = await self.service.patch_text(
            artifact_id=second.artifact_id,
            expected_current_artifact_id=second.artifact_id,
            access=access,
            cycle_id="cycle-1",
            operations=[
                ExactTextPatchOperation(
                    old_text="delta",
                    new_text="epsilon",
                    expected_occurrences=1,
                )
            ],
            provenance=provenance("patch", edit=True),
        )
        self.assertEqual(third.version, 3)
        final = await self.service.read_text(
            third.artifact_id,
            access=access,
            limit_chars=100,
        )
        self.assertEqual(final.text, "alpha beta epsilon")
        self.assertTrue(final.eof)

        versions = await self.artifacts.artifact_store.list_versions(
            first.artifact_lineage_id
        )
        self.assertEqual([item.version for item in versions], [1, 2, 3])
        self.assertNotEqual(versions[0].content_id, versions[1].content_id)

    async def test_stale_replace_does_not_advance_lineage(self):
        first = await self._create_markdown()
        access = self.access(first.artifact_id)
        second = await self.service.replace_text(
            artifact_id=first.artifact_id,
            expected_current_artifact_id=first.artifact_id,
            access=access,
            cycle_id="cycle-1",
            new_text="new",
            provenance=provenance("replace", edit=True),
        )

        with self.assertRaises(ArtifactVersionConflictError):
            await self.service.replace_text(
                artifact_id=first.artifact_id,
                expected_current_artifact_id=first.artifact_id,
                access=access,
                cycle_id="cycle-1",
                new_text="stale",
                provenance=provenance("replace", edit=True),
            )

        current = await self.artifacts.artifact_store.get_current_version(
            first.artifact_lineage_id
        )
        self.assertEqual(current.artifact_id, second.artifact_id)
        self.assertEqual(current.version, 2)

    async def test_patch_mismatch_is_atomic(self):
        first = await self._create_markdown("same same")
        access = self.access(first.artifact_id)

        with self.assertRaises(ArtifactValidationError) as raised:
            await self.service.patch_text(
                artifact_id=first.artifact_id,
                expected_current_artifact_id=first.artifact_id,
                access=access,
                cycle_id="cycle-1",
                operations=[
                    ExactTextPatchOperation(
                        old_text="same",
                        new_text="changed",
                        expected_occurrences=1,
                    )
                ],
                provenance=provenance("patch", edit=True),
            )
        self.assertEqual(raised.exception.code, "artifact_patch_conflict")
        versions = await self.artifacts.artifact_store.list_versions(
            first.artifact_lineage_id
        )
        self.assertEqual(len(versions), 1)

    async def test_invalid_json_is_rejected_before_artifact_commit(self):
        with self.assertRaises(ArtifactValidationError) as raised:
            await self.service.create_text(
                session_id="session-1",
                cycle_id="cycle-1",
                filename="invalid.json",
                text="{",
                format_id="json",
                provenance=provenance("create"),
            )
        self.assertEqual(raised.exception.code, "invalid_json_artifact")
        self.assertEqual(
            await self.artifacts.artifact_store.list_lineages(
                session_id="session-1"
            ),
            [],
        )

    async def test_access_is_session_and_lineage_scoped(self):
        first = await self._create_markdown()
        with self.assertRaises(ArtifactAccessError):
            await self.service.get_artifact(
                first.artifact_id,
                access=self.access(first.artifact_id, session_id="session-2"),
            )
        with self.assertRaises(ArtifactAccessError):
            await self.service.get_artifact(
                first.artifact_id,
                access=self.access(),
            )

    async def test_binary_format_rejects_text_read(self):
        content = await self.storage.content_store.save_content(
            b"%PDF-1.7\n",
            source_type="test",
            source_name="test.pdf",
            mime_type="application/pdf",
            cycle_id="cycle-1",
        )
        _, version = await self.artifacts.artifact_store.create_lineage(
            session_id="session-1",
            cycle_id="cycle-1",
            content_id=content.content_id,
            filename="test.pdf",
            format_id="pdf",
            detected_mime_type="application/pdf",
            declared_mime_type="application/pdf",
            provenance=ArtifactProvenance(
                origin="user_upload",
                creator="user",
                operation="ingress",
            ),
        )
        with self.assertRaises(ArtifactCapabilityError):
            await self.service.read_text(
                version.artifact_id,
                access=self.access(version.artifact_id),
            )


if __name__ == "__main__":
    unittest.main()
