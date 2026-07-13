import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.storage import StorageConfigType
from src.storage.errors import (
    StorageContentTooLargeError,
    StorageError,
    StorageIntegrityError,
    StorageSerializationError,
    StorageValidationError,
)
from src.storage import file_backend as file_backend_module
from src.storage.file_backend import FileSystemArtifactStore


class ArtifactStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = FileSystemArtifactStore(StorageConfigType(root_dir=str(self.root)))

    def tearDown(self):
        self.temporary.cleanup()

    def test_directory_fsync_is_best_effort_on_posix(self):
        with (
            patch.object(file_backend_module.os, "open", return_value=42) as open_mock,
            patch.object(file_backend_module.os, "fsync") as fsync_mock,
            patch.object(file_backend_module.os, "close") as close_mock,
            patch.object(file_backend_module.os, "name", "posix"),
        ):
            file_backend_module._fsync_directory(self.root)

        open_mock.assert_called_once_with(self.root, file_backend_module.os.O_RDONLY)
        fsync_mock.assert_called_once_with(42)
        close_mock.assert_called_once_with(42)

        with (
            patch.object(file_backend_module.os, "open", side_effect=OSError("unsupported")),
            patch.object(file_backend_module.os, "name", "posix"),
        ):
            file_backend_module._fsync_directory(self.root)

    async def test_initial_save_and_safe_filename(self):
        artifact = await self.store.save_artifact(
            b"report",
            cycle_id="cycle-1",
            filename="../../report.md",
            mime_type="text/markdown",
            source="agent_generated",
            metadata={"kind": "report"},
        )

        self.assertEqual(artifact.version, 1)
        self.assertIsNone(artifact.parent_artifact_id)
        self.assertEqual(artifact.filename, "report.md")
        self.assertEqual(artifact.mime_type, "text/markdown")
        self.assertEqual(await self.store.open_artifact(artifact.artifact_id), b"report")
        self.assertFalse(
            any("path" in key for key in type(artifact).model_fields)
        )
        self.assertEqual(
            sorted(path.name for path in self.root.iterdir()),
            ["artifacts", "contents", "cycles", "indexes", "input_batches", "plans"],
        )

        detected = await self.store.save_artifact(
            b"text",
            cycle_id="cycle-1",
            filename="note.txt",
            source="agent_generated",
        )
        self.assertEqual(detected.mime_type, "text/plain")

    async def test_public_validation_and_serialization_errors_are_managed(self):
        with self.assertRaises(StorageValidationError):
            await self.store.save_artifact(
                b"data",
                cycle_id="cycle-1",
                filename="../../",
                source="test",
            )
        with self.assertRaises(StorageValidationError):
            await self.store.save_artifact(
                b"data",
                cycle_id="",
                filename="file.bin",
                source="test",
            )
        with self.assertRaises(StorageSerializationError):
            await self.store.save_artifact(
                b"data",
                cycle_id="cycle-1",
                filename="file.bin",
                source="test",
                metadata={"not_json": object()},
            )
        self.assertEqual(list((self.root / "artifacts").iterdir()), [])

    async def test_versioning_is_immutable_and_overlays_metadata(self):
        original = await self.store.save_artifact(
            b"v1",
            cycle_id="cycle-1",
            filename="report.md",
            mime_type="text/markdown",
            source="user_upload",
            metadata={"owner": "user", "stage": 1},
        )
        version_two = await self.store.create_version(
            original.artifact_id,
            b"v2",
            metadata={"stage": 2},
        )
        await self.store.mark_for_delivery(version_two.artifact_id, client_type="telegram")
        version_three = await self.store.create_version(
            version_two.artifact_id,
            b"v3",
            filename="final.txt",
            mime_type="text/plain",
        )

        self.assertEqual((version_two.version, version_three.version), (2, 3))
        self.assertEqual(version_two.parent_artifact_id, original.artifact_id)
        self.assertEqual(version_three.parent_artifact_id, version_two.artifact_id)
        self.assertEqual(version_two.filename, original.filename)
        self.assertEqual(version_two.mime_type, original.mime_type)
        self.assertEqual(version_two.metadata, {"owner": "user", "stage": 2})
        self.assertEqual(version_three.delivery_targets, [])
        self.assertEqual(await self.store.open_artifact(original.artifact_id), b"v1")
        self.assertEqual(await self.store.open_artifact(version_two.artifact_id), b"v2")
        self.assertEqual(await self.store.open_artifact(version_three.artifact_id), b"v3")

    async def test_list_cycle_artifacts_is_filtered_and_deterministic(self):
        first = await self.store.save_artifact(
            b"one", cycle_id="cycle-1", filename="one.txt", source="test"
        )
        second = await self.store.create_version(first.artifact_id, b"two")
        await self.store.save_artifact(
            b"other", cycle_id="cycle-2", filename="other.txt", source="test"
        )

        listed = await self.store.list_cycle_artifacts("cycle-1")

        self.assertEqual({item.artifact_id for item in listed}, {first.artifact_id, second.artifact_id})
        self.assertEqual(listed, sorted(listed, key=lambda item: (item.created_at, item.version, item.artifact_id)))

    async def test_delivery_is_idempotent_and_concurrent_updates_are_not_lost(self):
        artifact = await self.store.save_artifact(
            b"file", cycle_id="cycle-1", filename="file.bin", source="test"
        )

        await asyncio.gather(
            self.store.mark_for_delivery(artifact.artifact_id, client_type="telegram"),
            self.store.mark_for_delivery(artifact.artifact_id, client_type="telegram"),
            self.store.mark_for_delivery(artifact.artifact_id, client_type="web"),
        )

        updated = await self.store.get_artifact(artifact.artifact_id)
        self.assertEqual(updated.delivery_targets, ["telegram", "web"])
        self.assertEqual(await self.store.open_artifact(artifact.artifact_id), b"file")
        self.assertEqual(self.store._metadata_locks, {})

    async def test_delivery_target_is_trimmed_before_idempotency_check(self):
        artifact = await self.store.save_artifact(
            b"file", cycle_id="cycle-1", filename="file.bin", source="test"
        )

        await self.store.mark_for_delivery(
            artifact.artifact_id,
            client_type=" telegram ",
        )
        await self.store.mark_for_delivery(
            artifact.artifact_id,
            client_type="telegram",
        )

        updated = await self.store.get_artifact(artifact.artifact_id)
        self.assertEqual(updated.delivery_targets, ["telegram"])
        self.assertEqual(self.store._metadata_locks, {})

    async def test_delivery_metadata_update_is_atomic(self):
        artifact = await self.store.save_artifact(
            b"file", cycle_id="cycle-1", filename="file.bin", source="test"
        )

        with patch("src.storage.file_backend.os.replace", side_effect=OSError("injected")):
            with self.assertRaises(StorageError):
                await self.store.mark_for_delivery(
                    artifact.artifact_id,
                    client_type="telegram",
                )

        unchanged = await self.store.get_artifact(artifact.artifact_id)
        self.assertEqual(unchanged.delivery_targets, [])
        self.assertEqual(self.store._metadata_locks, {})
        object_dir = self.root / "artifacts" / artifact.artifact_id
        self.assertFalse(any(path.name.startswith("metadata.json.tmp") for path in object_dir.iterdir()))

    async def test_integrity_and_memory_limit(self):
        artifact = await self.store.save_artifact(
            b"original", cycle_id="cycle-1", filename="file.bin", source="test"
        )
        binary_path = self.root / "artifacts" / artifact.artifact_id / "file.bin"
        binary_path.write_bytes(b"changed!")
        with self.assertRaises(StorageIntegrityError):
            await self.store.open_artifact(artifact.artifact_id)

        limited = FileSystemArtifactStore(
            StorageConfigType(
                root_dir=str(self.root / "limited"),
                max_in_memory_content_bytes=2,
            )
        )
        large = await limited.save_artifact(
            b"large", cycle_id="cycle-1", filename="large.bin", source="test"
        )
        with self.assertRaises(StorageContentTooLargeError):
            await limited.open_artifact(large.artifact_id)

    async def test_parallel_artifact_saves_have_unique_ids(self):
        artifacts = await asyncio.gather(
            *(
                self.store.save_artifact(
                    f"value-{index}".encode(),
                    cycle_id="cycle-1",
                    filename=f"file-{index}.txt",
                    source="test",
                )
                for index in range(10)
            )
        )

        self.assertEqual(len({item.artifact_id for item in artifacts}), len(artifacts))
        for index, artifact in enumerate(artifacts):
            self.assertEqual(
                await self.store.open_artifact(artifact.artifact_id),
                f"value-{index}".encode(),
            )
