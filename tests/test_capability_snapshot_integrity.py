import json
import tempfile
import unittest
from pathlib import Path

from src.interaction.capabilities import (
    build_default_capability_registry,
    build_telegram_capability_declaration,
)
from src.interaction.capability_store import (
    FileSystemCapabilitySnapshotStore,
)
from src.interaction.config import ClientCapabilitiesConfig
from src.interaction.errors import InteractionIntegrityError
from src.storage.config import StorageConfigType


class CapabilitySnapshotIntegrityTests(unittest.IsolatedAsyncioTestCase):
    async def _stored(self, temporary, instance="bot-1"):
        store = FileSystemCapabilitySnapshotStore(
            StorageConfigType(root_dir=temporary),
            build_default_capability_registry(),
            ClientCapabilitiesConfig(),
        )
        snapshot, _ = await store.resolve(
            build_telegram_capability_declaration(),
            client_type="telegram",
            client_instance_id=instance,
        )
        return store, snapshot

    async def test_limit_feature_and_instance_tampering_are_rejected(self):
        for mutation in ("limit", "feature", "instance"):
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as temporary:
                    store, snapshot = await self._stored(temporary)
                    path = (
                        store.snapshots_dir
                        / f"{snapshot.capability_snapshot_id}.json"
                    )
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    if mutation == "limit":
                        payload["limits"][
                            "transport.telegram.output.text.max_chars"
                        ] = 1
                    elif mutation == "feature":
                        payload["features"].append("output.unknown")
                        payload["features"].sort()
                    else:
                        payload["client_instance_id"] = "attacker"
                    path.write_text(
                        json.dumps(payload),
                        encoding="utf-8",
                    )
                    with self.assertRaises(InteractionIntegrityError):
                        await store.get(snapshot.capability_snapshot_id)

    async def test_fingerprint_index_cannot_point_to_another_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, first = await self._stored(temporary, "bot-1")
            second, _ = await store.resolve(
                build_telegram_capability_declaration(),
                client_type="telegram",
                client_instance_id="bot-2",
            )
            index_path = (
                store.fingerprints_dir
                / f"{first.fingerprint.removeprefix('sha256:')}.json"
            )
            pointer = json.loads(index_path.read_text(encoding="utf-8"))
            pointer["capability_snapshot_id"] = second.capability_snapshot_id
            index_path.write_text(json.dumps(pointer), encoding="utf-8")
            with self.assertRaises(InteractionIntegrityError):
                await store.get(first.capability_snapshot_id)

    async def test_snapshot_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            store, snapshot = await self._stored(temporary)
            path = (
                store.snapshots_dir
                / f"{snapshot.capability_snapshot_id}.json"
            )
            target = store.snapshots_dir / "target.json"
            path.replace(target)
            try:
                path.symlink_to(target)
            except OSError as error:
                self.skipTest(f"symlink creation unavailable: {error}")
            with self.assertRaises(InteractionIntegrityError):
                await store.get(snapshot.capability_snapshot_id)
