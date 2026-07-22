import tempfile
import unittest
from pathlib import Path

from src.artifacts import (
    ArtifactAccessContext,
    ArtifactConfigType,
    ArtifactDeliveryError,
    ArtifactDeliveryState,
    ArtifactProvenance,
    ArtifactPurpose,
    create_artifact_services,
)
from src.storage import StorageConfigType, create_storage_services


class ArtifactDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.storage_config = StorageConfigType(root_dir=str(root / "storage"))
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
        self.access = ArtifactAccessContext(
            session_id="session-1",
            cycle_id="cycle-1",
            allowed_artifact_ids=[],
        )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def _artifact(self, text: str = "report"):
        item = await self.services.artifact_service.create_text(
            session_id="session-1",
            cycle_id="cycle-1",
            filename="report.md",
            text=text,
            format_id="markdown",
            purpose=ArtifactPurpose.DELIVERABLE,
            provenance=ArtifactProvenance(
                origin="agent_created",
                creator="agent",
                operation="delivery_test",
            ),
        )
        self.access.allowed_artifact_ids.append(item.artifact_id)
        return item

    async def test_selection_is_idempotent_and_persistent(self):
        artifact = await self._artifact()

        first = await self.services.delivery_service.select(
            artifact_id=artifact.artifact_id,
            access=self.access,
            client_type="telegram",
        )
        second = await self.services.delivery_service.select(
            artifact_id=artifact.artifact_id,
            access=self.access,
            client_type="telegram",
        )

        self.assertEqual(first.delivery_id, second.delivery_id)
        self.assertEqual(first.state, ArtifactDeliveryState.SELECTED)
        reloaded = await self.services.delivery_store.get(first.delivery_id)
        self.assertEqual(reloaded.artifact_id, artifact.artifact_id)
        self.assertEqual(reloaded.state, ArtifactDeliveryState.SELECTED)

    async def test_delivery_state_machine_and_idempotent_receipt(self):
        artifact = await self._artifact()
        selected = await self.services.delivery_service.select(
            artifact_id=artifact.artifact_id,
            access=self.access,
            client_type="web",
        )

        delivering = await self.services.delivery_service.claim(
            selected.delivery_id
        )
        self.assertEqual(delivering.state, ArtifactDeliveryState.DELIVERING)

        delivered = await self.services.delivery_service.complete(
            selected.delivery_id,
            receipt={"client_message_id": "message-1"},
        )
        repeated = await self.services.delivery_service.complete(
            selected.delivery_id,
            receipt={"client_message_id": "message-1"},
        )
        self.assertEqual(delivered.state, ArtifactDeliveryState.DELIVERED)
        self.assertEqual(repeated.state, ArtifactDeliveryState.DELIVERED)
        record = await self.services.delivery_store.get(selected.delivery_id)
        self.assertEqual(record.receipt["client_message_id"], "message-1")
        self.assertEqual(record.attempt_count, 1)

        with self.assertRaises(ArtifactDeliveryError):
            await self.services.delivery_service.claim(selected.delivery_id)

    async def test_failure_and_ambiguous_timeout_are_distinct(self):
        artifact = await self._artifact()
        failed_ref = await self.services.delivery_service.select(
            artifact_id=artifact.artifact_id,
            access=self.access,
            client_type="telegram",
        )
        await self.services.delivery_service.claim(failed_ref.delivery_id)
        failed = await self.services.delivery_service.fail(
            failed_ref.delivery_id,
            error="connection refused",
        )
        self.assertEqual(failed.state, ArtifactDeliveryState.FAILED)

        retrying = await self.services.delivery_service.claim(
            failed_ref.delivery_id
        )
        self.assertEqual(retrying.state, ArtifactDeliveryState.DELIVERING)
        unknown = await self.services.delivery_service.fail(
            failed_ref.delivery_id,
            error="timeout after upload started",
            ambiguous=True,
        )
        self.assertEqual(unknown.state, ArtifactDeliveryState.UNKNOWN)

    async def test_selecting_new_lineage_head_cancels_old_pending_selection(self):
        first = await self._artifact("v1")
        old_delivery = await self.services.delivery_service.select(
            artifact_id=first.artifact_id,
            access=self.access,
            client_type="telegram",
        )
        second = await self.services.artifact_service.replace_text(
            artifact_id=first.artifact_id,
            expected_current_artifact_id=first.artifact_id,
            access=self.access,
            cycle_id="cycle-1",
            new_text="v2",
            provenance=ArtifactProvenance(
                origin="agent_edit",
                creator="agent",
                source_artifact_ids=[first.artifact_id],
                operation="delivery_test_replace",
            ),
        )
        self.access.allowed_artifact_ids.append(second.artifact_id)

        new_delivery = await self.services.delivery_service.select(
            artifact_id=second.artifact_id,
            access=self.access,
            client_type="telegram",
        )

        old_record = await self.services.delivery_store.get(
            old_delivery.delivery_id
        )
        self.assertEqual(old_record.state, ArtifactDeliveryState.CANCELLED)
        self.assertEqual(new_delivery.artifact_id, second.artifact_id)
        refs = await self.services.delivery_service.list_cycle_refs(
            session_id="session-1",
            cycle_id="cycle-1",
        )
        self.assertEqual([item.delivery_id for item in refs], [new_delivery.delivery_id])

    async def test_content_is_streamed_from_canonical_content_store(self):
        artifact = await self._artifact("alpha beta gamma")
        selected = await self.services.delivery_service.select(
            artifact_id=artifact.artifact_id,
            access=self.access,
            client_type="web",
        )

        chunks = []
        async for chunk in self.services.delivery_service.iter_content(
            selected.delivery_id,
            session_id="session-1",
            client_type="web",
            chunk_size=4,
        ):
            chunks.append(chunk)
        self.assertEqual(b"".join(chunks), b"alpha beta gamma")


if __name__ == "__main__":
    unittest.main()
