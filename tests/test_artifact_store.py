import asyncio
import hashlib
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from src.artifacts import (
    ArtifactConfigType,
    ArtifactIntegrityError,
    ArtifactProvenance,
    ArtifactPurpose,
    ArtifactStorageError,
    ArtifactVersionConflictError,
    create_artifact_services,
)
from src.artifacts.file_store import FileSystemArtifactStore
from src.artifacts.migration import LegacyArtifactMigrator
from src.storage import StorageConfigType, create_storage_services
from src.storage.models import ArtifactRef as LegacyArtifactRef
from src.storage.serializers import serialize_model


class ArtifactLineageStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.storage_config = StorageConfigType(root_dir=str(self.root))
        self.storage = create_storage_services(self.storage_config)
        self.artifacts = create_artifact_services(
            storage_config=self.storage_config,
            artifact_config=ArtifactConfigType(),
            content_store=self.storage.content_store,
        )
        self.store = self.artifacts.artifact_store

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def provenance(operation="test"):
        return ArtifactProvenance(
            origin="agent_created",
            creator="agent",
            operation=operation,
        )

    async def save_content(self, value: bytes | str):
        return await self.storage.content_store.save_content(
            value,
            source_type="test",
        )

    async def create_initial(self):
        content = await self.save_content("version one")
        return await self.store.create_lineage(
            session_id="session-1",
            cycle_id="cycle-1",
            content_id=content.content_id,
            filename="../../report.md",
            format_id="markdown",
            detected_mime_type="text/markdown",
            declared_mime_type="text/markdown",
            encoding="utf-8",
            provenance=self.provenance("create"),
            purpose=ArtifactPurpose.WORKING,
        )

    async def test_initial_lineage_references_content_without_copying_payload(self):
        lineage, version = await self.create_initial()

        self.assertEqual(lineage.current_artifact_id, version.artifact_id)
        self.assertEqual(version.version, 1)
        self.assertEqual(version.filename, "report.md")
        self.assertEqual(
            await self.storage.content_store.read_text(version.content_id),
            "version one",
        )

        version_dir = (
            self.root / "artifacts" / "versions" / version.artifact_id
        )
        self.assertEqual(
            sorted(path.name for path in version_dir.iterdir()),
            ["metadata.json"],
        )
        self.assertFalse((version_dir / "file.bin").exists())

    async def test_linear_versioning_and_exact_current_head(self):
        lineage, first = await self.create_initial()
        second_content = await self.save_content("version two")
        lineage, second = await self.store.create_version(
            artifact_lineage_id=lineage.artifact_lineage_id,
            expected_current_artifact_id=first.artifact_id,
            cycle_id="cycle-2",
            content_id=second_content.content_id,
            filename=None,
            format_id=None,
            detected_mime_type=None,
            provenance=self.provenance("replace"),
        )

        self.assertEqual(second.version, 2)
        self.assertEqual(second.parent_artifact_id, first.artifact_id)
        self.assertEqual(lineage.current_artifact_id, second.artifact_id)
        self.assertEqual(
            [item.artifact_id for item in await self.store.list_versions(
                lineage.artifact_lineage_id
            )],
            [first.artifact_id, second.artifact_id],
        )
        self.assertEqual(
            (await self.store.get_current_version(
                lineage.artifact_lineage_id
            )).artifact_id,
            second.artifact_id,
        )
        self.assertEqual(
            {item.artifact_id for item in await self.store.list_cycle_artifacts(
                "cycle-2"
            )},
            {second.artifact_id},
        )

    async def test_stale_and_parallel_mutations_do_not_fork_lineage(self):
        lineage, first = await self.create_initial()
        contents = await asyncio.gather(
            self.save_content("two-a"),
            self.save_content("two-b"),
        )

        async def append(content):
            return await self.store.create_version(
                artifact_lineage_id=lineage.artifact_lineage_id,
                expected_current_artifact_id=first.artifact_id,
                cycle_id="cycle-2",
                content_id=content.content_id,
                filename=None,
                format_id=None,
                detected_mime_type=None,
                provenance=self.provenance("parallel"),
            )

        outcomes = await asyncio.gather(
            *(append(content) for content in contents),
            return_exceptions=True,
        )
        successes = [
            item for item in outcomes if not isinstance(item, Exception)
        ]
        conflicts = [
            item
            for item in outcomes
            if isinstance(item, ArtifactVersionConflictError)
        ]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(conflicts), 1)

        current = await self.store.get_lineage(
            lineage.artifact_lineage_id
        )
        self.assertEqual(current.current_version, 2)
        self.assertEqual(len(current.committed_artifact_ids), 2)
        self.assertEqual(self.store._lineage_locks, {})

    async def test_failed_manifest_replace_keeps_old_head_and_hides_orphan(self):
        lineage, first = await self.create_initial()
        content = await self.save_content("version two")

        with patch.object(
            self.store,
            "_replace_lineage_metadata",
            side_effect=ArtifactStorageError("injected"),
        ):
            with self.assertRaises(ArtifactStorageError):
                await self.store.create_version(
                    artifact_lineage_id=lineage.artifact_lineage_id,
                    expected_current_artifact_id=first.artifact_id,
                    cycle_id="cycle-2",
                    content_id=content.content_id,
                    filename=None,
                    format_id=None,
                    detected_mime_type=None,
                    provenance=self.provenance("replace"),
                )

        current = await self.store.get_lineage(
            lineage.artifact_lineage_id
        )
        self.assertEqual(current.current_artifact_id, first.artifact_id)
        orphan_ids = await self.store.list_orphan_version_ids()
        self.assertEqual(len(orphan_ids), 1)
        with self.assertRaises(Exception):
            await self.store.get_version(orphan_ids[0])

    async def test_content_metadata_mismatch_is_detected(self):
        lineage, version = await self.create_initial()
        metadata_path = (
            self.root / "contents" / version.content_id / "metadata.json"
        )
        payload = metadata_path.read_text("utf-8")
        metadata_path.write_text(
            payload.replace(
                version.content_hash,
                "sha256:" + "f" * 64,
            ),
            "utf-8",
        )
        with self.assertRaises(ArtifactIntegrityError):
            await self.store.get_version(version.artifact_id)

    async def test_archive_and_session_listing(self):
        lineage, first = await self.create_initial()
        self.assertEqual(
            len(await self.store.list_lineages(session_id="session-1")),
            1,
        )
        archived = await self.store.archive_lineage(
            lineage.artifact_lineage_id,
            expected_current_artifact_id=first.artifact_id,
        )
        self.assertEqual(archived.status.value, "archived")
        self.assertEqual(
            await self.store.list_lineages(session_id="session-1"),
            [],
        )
        self.assertEqual(
            len(await self.store.list_lineages(
                session_id="session-1",
                include_archived=True,
            )),
            1,
        )


class LegacyArtifactMigrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.storage_config = StorageConfigType(root_dir=str(self.root))
        self.storage = create_storage_services(self.storage_config)

    def tearDown(self):
        self.temporary.cleanup()

    def write_legacy(
        self,
        *,
        artifact_id: str,
        payload: bytes,
        version: int,
        parent_artifact_id: str | None,
        delivery_targets=None,
    ):
        object_dir = self.root / "artifacts" / artifact_id
        object_dir.mkdir(parents=True, exist_ok=False)
        (object_dir / "file.bin").write_bytes(payload)
        metadata = LegacyArtifactRef(
            artifact_id=artifact_id,
            cycle_id="legacy-cycle",
            filename=f"report-v{version}.txt",
            mime_type="text/plain",
            size_bytes=len(payload),
            content_hash=(
                "sha256:" + hashlib.sha256(payload).hexdigest()
            ),
            version=version,
            parent_artifact_id=parent_artifact_id,
            source="user_upload",
            created_at=datetime.now(timezone.utc),
            metadata={"session_id": "session-legacy"},
            delivery_targets=list(delivery_targets or []),
        )
        (object_dir / "metadata.json").write_bytes(
            serialize_model(
                metadata,
                object_type="artifact",
                object_id=artifact_id,
            )
        )

    async def test_legacy_layout_blocks_normal_writes_until_migration(self):
        artifact_id = "art_" + "1" * 32
        self.write_legacy(
            artifact_id=artifact_id,
            payload=b"legacy",
            version=1,
            parent_artifact_id=None,
        )
        store = FileSystemArtifactStore(
            storage_config=self.storage_config,
            artifact_config=ArtifactConfigType(),
            content_store=self.storage.content_store,
        )
        content = await self.storage.content_store.save_content(
            b"new",
            source_type="test",
        )
        with self.assertRaises(ArtifactStorageError):
            await store.create_lineage(
                session_id="session",
                cycle_id="cycle",
                content_id=content.content_id,
                filename="new.bin",
                format_id="opaque_binary",
                detected_mime_type="application/octet-stream",
                provenance=ArtifactProvenance(
                    origin="agent_created",
                    creator="agent",
                    operation="create",
                ),
            )

    async def test_dry_run_apply_and_idempotent_migration(self):
        first_id = "art_" + "1" * 32
        second_id = "art_" + "2" * 32
        self.write_legacy(
            artifact_id=first_id,
            payload=b"one",
            version=1,
            parent_artifact_id=None,
        )
        self.write_legacy(
            artifact_id=second_id,
            payload=b"two",
            version=2,
            parent_artifact_id=first_id,
            delivery_targets=["telegram"],
        )
        store = FileSystemArtifactStore(
            storage_config=self.storage_config,
            artifact_config=ArtifactConfigType(),
            content_store=self.storage.content_store,
            allow_legacy_layout=True,
        )
        migrator = LegacyArtifactMigrator(
            artifact_store=store,
            content_store=self.storage.content_store,
        )

        dry = await migrator.migrate(dry_run=True)
        self.assertEqual(dry.discovered_versions, 2)
        self.assertEqual(dry.discovered_lineages, 1)
        self.assertEqual(dry.errors, [])
        self.assertEqual(list(store.lineages_dir.iterdir()), [])

        applied = await migrator.migrate(dry_run=False)
        self.assertEqual(applied.migrated_lineages, 1)
        self.assertEqual(applied.migrated_versions, 2)
        self.assertEqual(applied.errors, [])

        lineages = await store.list_lineages(
            session_id="session-legacy"
        )
        self.assertEqual(len(lineages), 1)
        self.assertEqual(lineages[0].purpose, ArtifactPurpose.DELIVERABLE)
        versions = await store.list_versions(
            lineages[0].artifact_lineage_id
        )
        self.assertEqual(
            [item.artifact_id for item in versions],
            [first_id, second_id],
        )
        self.assertEqual(
            await self.storage.content_store.read_content(
                versions[0].content_id
            ),
            b"one",
        )
        self.assertTrue(
            (self.root / "artifacts" / first_id / "file.bin").exists()
        )

        repeated = await migrator.migrate(dry_run=False)
        self.assertEqual(repeated.skipped_lineages, 1)
        self.assertEqual(repeated.migrated_lineages, 0)
        self.assertEqual(repeated.errors, [])
