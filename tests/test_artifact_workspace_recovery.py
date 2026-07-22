import os
import tempfile
import time
import unittest
from pathlib import Path

from src.artifacts import (
    ArtifactConfigType,
    cleanup_stale_artifact_workspaces,
    create_artifact_services,
)
from src.storage import StorageConfigType, create_storage_services


class ArtifactWorkspaceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        storage_config = StorageConfigType(root_dir=str(self.root / "storage"))
        storage = create_storage_services(storage_config)
        self.services = create_artifact_services(
            storage_config=storage_config,
            artifact_config=ArtifactConfigType(
                max_artifact_size_bytes=1024 * 1024,
                max_patchable_text_bytes=1024 * 1024,
                max_workspace_bytes=2 * 1024 * 1024,
                workspace_ttl_seconds=60,
            ),
            content_store=storage.content_store,
        )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_only_stale_workspace_entries_are_removed(self):
        now = time.time()
        stale = self.services.workspace_manager.root / "tool-stale"
        fresh = self.services.workspace_manager.root / "tool-fresh"
        stale.mkdir()
        fresh.mkdir()
        (stale / "input.bin").write_bytes(b"stale")
        (fresh / "input.bin").write_bytes(b"fresh")
        os.utime(stale, (now - 120, now - 120))
        os.utime(fresh, (now - 10, now - 10))

        removed = await cleanup_stale_artifact_workspaces(
            self.services.workspace_manager,
            ttl_seconds=60,
            now_timestamp=now,
        )

        self.assertEqual(removed, ["tool-stale"])
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())

    async def test_stale_symlink_is_unlinked_without_touching_target(self):
        outside = self.root / "outside"
        outside.mkdir()
        marker = outside / "marker.txt"
        marker.write_text("keep", encoding="utf-8")
        link = self.services.workspace_manager.root / "tool-link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation is unavailable")

        removed = await cleanup_stale_artifact_workspaces(
            self.services.workspace_manager,
            ttl_seconds=1,
            now_timestamp=time.time() + 10,
        )

        self.assertEqual(removed, ["tool-link"])
        self.assertFalse(link.exists())
        self.assertTrue(marker.exists())

    async def test_invalid_ttl_is_rejected(self):
        with self.assertRaises(ValueError):
            await cleanup_stale_artifact_workspaces(
                self.services.workspace_manager,
                ttl_seconds=0,
            )


if __name__ == "__main__":
    unittest.main()
