import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from src.artifacts import (
    ArtifactAccessContext,
    ArtifactConfigType,
    ArtifactDeliveryState,
    ArtifactProvenance,
    create_artifact_services,
    recover_stale_delivery_claims,
    utc_now,
)
from src.storage import StorageConfigType, create_storage_services


class ArtifactDeliveryRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        storage_config = StorageConfigType(root_dir=str(root / "storage"))
        storage = create_storage_services(storage_config)
        self.services = create_artifact_services(
            storage_config=storage_config,
            artifact_config=ArtifactConfigType(
                max_artifact_size_bytes=1024 * 1024,
                max_patchable_text_bytes=1024 * 1024,
                max_workspace_bytes=2 * 1024 * 1024,
                delivery_claim_timeout_seconds=30,
            ),
            content_store=storage.content_store,
        )
        artifact = await self.services.artifact_service.create_text(
            session_id="session-1",
            cycle_id="cycle-1",
            filename="report.md",
            text="report",
            format_id="markdown",
            provenance=ArtifactProvenance(
                origin="agent_created",
                creator="agent",
                operation="delivery_recovery_test",
            ),
        )
        self.access = ArtifactAccessContext(
            session_id="session-1",
            cycle_id="cycle-1",
            allowed_artifact_ids=[artifact.artifact_id],
        )
        self.delivery = await self.services.delivery_service.select(
            artifact_id=artifact.artifact_id,
            access=self.access,
            client_type="telegram",
        )
        await self.services.delivery_service.claim(self.delivery.delivery_id)

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_stale_claim_becomes_unknown_without_new_attempt(self):
        claimed = await self.services.delivery_store.get(self.delivery.delivery_id)
        future = claimed.delivering_at + timedelta(seconds=31)

        recovered = await recover_stale_delivery_claims(
            self.services.delivery_store,
            claim_timeout_seconds=30,
            now=future,
        )

        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].state, ArtifactDeliveryState.UNKNOWN)
        record = await self.services.delivery_store.get(self.delivery.delivery_id)
        self.assertEqual(record.state, ArtifactDeliveryState.UNKNOWN)
        self.assertEqual(record.attempt_count, 1)
        self.assertEqual(
            record.last_error,
            "delivery_claim_recovered_after_timeout",
        )
        self.assertEqual(record.receipt["recovery"], "startup_stale_claim")

        repeated = await recover_stale_delivery_claims(
            self.services.delivery_store,
            claim_timeout_seconds=30,
            now=future + timedelta(seconds=30),
        )
        self.assertEqual(repeated, [])
        record = await self.services.delivery_store.get(self.delivery.delivery_id)
        self.assertEqual(record.attempt_count, 1)

    async def test_fresh_claim_remains_delivering(self):
        claimed = await self.services.delivery_store.get(self.delivery.delivery_id)
        recovered = await recover_stale_delivery_claims(
            self.services.delivery_store,
            claim_timeout_seconds=30,
            now=claimed.delivering_at + timedelta(seconds=29),
        )
        self.assertEqual(recovered, [])
        record = await self.services.delivery_store.get(self.delivery.delivery_id)
        self.assertEqual(record.state, ArtifactDeliveryState.DELIVERING)

    async def test_invalid_recovery_clock_and_timeout_are_rejected(self):
        with self.assertRaises(ValueError):
            await recover_stale_delivery_claims(
                self.services.delivery_store,
                claim_timeout_seconds=0,
            )
        with self.assertRaises(ValueError):
            await recover_stale_delivery_claims(
                self.services.delivery_store,
                claim_timeout_seconds=30,
                now=utc_now().replace(tzinfo=None),
            )


if __name__ == "__main__":
    unittest.main()
