import asyncio
import tempfile
import unittest
from pathlib import Path

from src.artifacts import (
    ArtifactAccessContext,
    ArtifactAccessError,
    ArtifactCandidate,
    ArtifactCandidateError,
    ArtifactCandidateStatus,
    ArtifactCapabilityError,
    ArtifactConfigType,
    ArtifactIntegrityError,
    ArtifactProvenance,
    ArtifactPurpose,
    ArtifactValidationError,
    create_artifact_services,
    new_artifact_candidate_id,
    utc_now,
)
from src.storage import StorageConfigType, create_storage_services


class FailOncePromotionStore:
    def __init__(self, wrapped):
        self.wrapped = wrapped
        self.failures_remaining = 1

    async def create(self, candidate):
        return await self.wrapped.create(candidate)

    async def get(self, candidate_id):
        return await self.wrapped.get(candidate_id)

    async def list_cycle(self, **kwargs):
        return await self.wrapped.list_cycle(**kwargs)

    async def mark_promoted(self, candidate_id, *, artifact_id):
        if self.failures_remaining:
            self.failures_remaining -= 1
            raise ArtifactCandidateError("simulated terminal-state failure")
        return await self.wrapped.mark_promoted(
            candidate_id,
            artifact_id=artifact_id,
        )

    async def mark_discarded(self, candidate_id):
        return await self.wrapped.mark_discarded(candidate_id)


class ArtifactCandidatePromotionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.storage_config = StorageConfigType(
            root_dir=str(self.root / "storage")
        )
        self.storage = create_storage_services(self.storage_config)
        self.services = create_artifact_services(
            storage_config=self.storage_config,
            artifact_config=ArtifactConfigType(
                max_artifact_size_bytes=1024 * 1024,
                max_patchable_text_bytes=1024 * 1024,
                max_workspace_bytes=2 * 1024 * 1024,
            ),
            content_store=self.storage.content_store,
        )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def _candidate(
        self,
        payload: bytes,
        *,
        filename: str = "result.md",
        format_id: str = "markdown",
        mime_type: str = "text/markdown",
        session_id: str = "session-1",
        cycle_id: str = "cycle-1",
        source_artifact_ids: list[str] | None = None,
        content_hash_override: str | None = None,
    ) -> ArtifactCandidate:
        content = await self.storage.content_store.save_content(
            payload,
            source_type="artifact_candidate",
            source_name=filename,
            mime_type=mime_type,
            cycle_id=cycle_id,
            tool_call_id="tool-call-1",
            metadata={"artifact_format_id": format_id},
        )
        candidate = ArtifactCandidate(
            candidate_id=new_artifact_candidate_id(),
            session_id=session_id,
            cycle_id=cycle_id,
            content_id=content.content_id,
            suggested_filename=filename,
            format_id=format_id,
            mime_type=mime_type,
            size_bytes=content.size_bytes,
            content_hash=content_hash_override or content.content_hash,
            source_tool_call_id="tool-call-1",
            source_tool_name="document_processor",
            source_artifact_ids=list(source_artifact_ids or []),
            status=ArtifactCandidateStatus.AVAILABLE,
            created_at=utc_now(),
            metadata={"processor": "test"},
        )
        return await self.services.candidate_store.create(candidate)

    async def test_promote_to_new_artifact_reuses_canonical_content(self):
        candidate = await self._candidate(b"processed text")

        artifact = await self.services.promotion_service.create_artifact(
            candidate_id=candidate.candidate_id,
            allowed_candidate_ids=[candidate.candidate_id],
            session_id="session-1",
            cycle_id="cycle-1",
            purpose=ArtifactPurpose.DELIVERABLE,
            title="Processed report",
            plan_id="plan_" + "a" * 32,
            plan_revision=2,
            plan_node_id="node-1",
        )

        version = await self.services.artifact_store.get_version(
            artifact.artifact_id
        )
        lineage = await self.services.artifact_store.get_lineage(
            artifact.artifact_lineage_id
        )
        promoted = await self.services.candidate_store.get(
            candidate.candidate_id
        )

        self.assertEqual(version.content_id, candidate.content_id)
        self.assertEqual(version.content_hash, candidate.content_hash)
        self.assertEqual(version.metadata["source_candidate_id"], candidate.candidate_id)
        self.assertEqual(version.provenance.origin, "tool_output")
        self.assertEqual(version.provenance.creator, "tool")
        self.assertEqual(version.provenance.tool_call_id, "tool-call-1")
        self.assertEqual(version.provenance.plan_revision, 2)
        self.assertEqual(lineage.purpose, ArtifactPurpose.DELIVERABLE)
        self.assertEqual(lineage.title, "Processed report")
        self.assertEqual(promoted.status, ArtifactCandidateStatus.PROMOTED)
        self.assertEqual(promoted.promoted_artifact_id, artifact.artifact_id)

        content_dirs = [
            item for item in (Path(self.storage_config.root_dir) / "contents").iterdir()
            if item.is_dir() and not item.name.startswith(".")
        ]
        self.assertEqual(len(content_dirs), 1)

    async def test_promote_to_new_version_preserves_format_and_lineage(self):
        original = await self.services.artifact_service.create_text(
            session_id="session-1",
            cycle_id="cycle-1",
            filename="report.md",
            text="v1",
            format_id="markdown",
            provenance=ArtifactProvenance(
                origin="agent_created",
                creator="agent",
                operation="create_test_artifact",
            ),
        )
        candidate = await self._candidate(
            b"v2",
            source_artifact_ids=[original.artifact_id],
        )
        access = ArtifactAccessContext(
            session_id="session-1",
            cycle_id="cycle-1",
            allowed_artifact_ids=[original.artifact_id],
        )

        promoted = await self.services.promotion_service.create_version(
            candidate_id=candidate.candidate_id,
            allowed_candidate_ids=[candidate.candidate_id],
            artifact_lineage_id=original.artifact_lineage_id,
            expected_current_artifact_id=original.artifact_id,
            access=access,
            cycle_id="cycle-1",
        )

        self.assertEqual(promoted.artifact_lineage_id, original.artifact_lineage_id)
        self.assertEqual(promoted.version, 2)
        version = await self.services.artifact_store.get_version(
            promoted.artifact_id
        )
        self.assertEqual(version.parent_artifact_id, original.artifact_id)
        self.assertEqual(version.content_id, candidate.content_id)
        self.assertEqual(version.provenance.source_artifact_ids, [original.artifact_id])
        versions = await self.services.artifact_store.list_versions(
            original.artifact_lineage_id
        )
        self.assertEqual([item.version for item in versions], [1, 2])

    async def test_format_mismatch_and_authority_fail_without_mutation(self):
        original = await self.services.artifact_service.create_text(
            session_id="session-1",
            cycle_id="cycle-1",
            filename="report.md",
            text="v1",
            format_id="markdown",
            provenance=ArtifactProvenance(
                origin="agent_created",
                creator="agent",
                operation="create_test_artifact",
            ),
        )
        pdf = await self._candidate(
            b"%PDF-1.7\n",
            filename="result.pdf",
            format_id="pdf",
            mime_type="application/pdf",
        )
        access = ArtifactAccessContext(
            session_id="session-1",
            cycle_id="cycle-1",
            allowed_artifact_ids=[original.artifact_id],
        )

        with self.assertRaises(ArtifactAccessError):
            await self.services.promotion_service.create_artifact(
                candidate_id=pdf.candidate_id,
                allowed_candidate_ids=[],
                session_id="session-1",
                cycle_id="cycle-1",
                purpose=ArtifactPurpose.WORKING,
            )

        with self.assertRaises(ArtifactCapabilityError):
            await self.services.promotion_service.create_version(
                candidate_id=pdf.candidate_id,
                allowed_candidate_ids=[pdf.candidate_id],
                artifact_lineage_id=original.artifact_lineage_id,
                expected_current_artifact_id=original.artifact_id,
                access=access,
                cycle_id="cycle-1",
            )

        current = await self.services.artifact_store.get_current_version(
            original.artifact_lineage_id
        )
        self.assertEqual(current.artifact_id, original.artifact_id)
        candidate = await self.services.candidate_store.get(pdf.candidate_id)
        self.assertEqual(candidate.status, ArtifactCandidateStatus.AVAILABLE)

    async def test_integrity_and_native_validation_fail_before_commit(self):
        corrupt_hash = "sha256:" + "f" * 64
        inconsistent = await self._candidate(
            b"text",
            content_hash_override=corrupt_hash,
        )
        invalid_json = await self._candidate(
            b"{not json",
            filename="result.json",
            format_id="json",
            mime_type="application/json",
        )

        with self.assertRaises(ArtifactIntegrityError):
            await self.services.promotion_service.create_artifact(
                candidate_id=inconsistent.candidate_id,
                allowed_candidate_ids=[inconsistent.candidate_id],
                session_id="session-1",
                cycle_id="cycle-1",
                purpose=ArtifactPurpose.WORKING,
            )
        with self.assertRaises(ArtifactValidationError):
            await self.services.promotion_service.create_artifact(
                candidate_id=invalid_json.candidate_id,
                allowed_candidate_ids=[invalid_json.candidate_id],
                session_id="session-1",
                cycle_id="cycle-1",
                purpose=ArtifactPurpose.WORKING,
            )

        self.assertEqual(
            await self.services.artifact_store.list_lineages(
                session_id="session-1",
                include_archived=True,
            ),
            [],
        )

    async def test_concurrent_promotion_returns_one_exact_artifact(self):
        candidate = await self._candidate(b"processed text")

        results = await asyncio.gather(*[
            self.services.promotion_service.create_artifact(
                candidate_id=candidate.candidate_id,
                allowed_candidate_ids=[candidate.candidate_id],
                session_id="session-1",
                cycle_id="cycle-1",
                purpose=ArtifactPurpose.WORKING,
            )
            for _ in range(3)
        ])

        self.assertEqual(len({item.artifact_id for item in results}), 1)
        lineages = await self.services.artifact_store.list_lineages(
            session_id="session-1",
            include_archived=True,
        )
        self.assertEqual(len(lineages), 1)
        self.assertEqual(lineages[0].current_version, 1)

    async def test_retry_repairs_candidate_after_artifact_commit(self):
        candidate = await self._candidate(b"processed text")
        failing_store = FailOncePromotionStore(self.services.candidate_store)
        from src.artifacts.promotion import ArtifactCandidatePromotionService

        service = ArtifactCandidatePromotionService(
            artifact_service=self.services.artifact_service,
            candidate_store=failing_store,
        )

        with self.assertRaises(ArtifactCandidateError):
            await service.create_artifact(
                candidate_id=candidate.candidate_id,
                allowed_candidate_ids=[candidate.candidate_id],
                session_id="session-1",
                cycle_id="cycle-1",
                purpose=ArtifactPurpose.WORKING,
            )

        lineages_after_failure = await self.services.artifact_store.list_lineages(
            session_id="session-1",
            include_archived=True,
        )
        self.assertEqual(len(lineages_after_failure), 1)
        still_available = await self.services.candidate_store.get(
            candidate.candidate_id
        )
        self.assertEqual(still_available.status, ArtifactCandidateStatus.AVAILABLE)

        recovered = await service.create_artifact(
            candidate_id=candidate.candidate_id,
            allowed_candidate_ids=[candidate.candidate_id],
            session_id="session-1",
            cycle_id="cycle-1",
            purpose=ArtifactPurpose.WORKING,
        )

        lineages_after_retry = await self.services.artifact_store.list_lineages(
            session_id="session-1",
            include_archived=True,
        )
        self.assertEqual(len(lineages_after_retry), 1)
        self.assertEqual(
            recovered.artifact_id,
            lineages_after_retry[0].current_artifact_id,
        )
        repaired = await self.services.candidate_store.get(candidate.candidate_id)
        self.assertEqual(repaired.status, ArtifactCandidateStatus.PROMOTED)
        self.assertEqual(repaired.promoted_artifact_id, recovered.artifact_id)


if __name__ == "__main__":
    unittest.main()
