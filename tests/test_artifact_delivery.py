import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from src.artifacts import (
    ArtifactAccessContext,
    ArtifactConfigType,
    ArtifactDeliveryError,
    ArtifactDeliveryNotFoundError,
    ArtifactDeliveryState,
    ArtifactNotFoundError,
    ArtifactProvenance,
    ArtifactPurpose,
    ArtifactStorageError,
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
        self.artifact_number = 0

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def _artifact(self, text: str = "report"):
        self.artifact_number += 1
        item = await self.services.artifact_service.create_text(
            session_id="session-1",
            cycle_id="cycle-1",
            filename=f"report-{self.artifact_number}.md",
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

    async def test_batch_selection_is_atomic_and_ordered(self):
        artifacts = [await self._artifact(f"report {index}") for index in range(4)]
        selected = await self.services.delivery_service.select_many(
            artifact_ids=[item.artifact_id for item in artifacts],
            access=self.access,
            client_type="telegram",
        )

        self.assertEqual(
            [item.artifact_id for item in selected],
            [item.artifact_id for item in artifacts],
        )
        records = await self.services.delivery_store.list_cycle(
            session_id="session-1",
            cycle_id="cycle-1",
        )
        self.assertEqual(len(records), 4)
        self.assertTrue(all(
            item.state == ArtifactDeliveryState.SELECTED for item in records
        ))

    async def test_invalid_batch_target_leaves_store_unchanged(self):
        artifacts = [await self._artifact(f"report {index}") for index in range(3)]
        unknown = "art_" + "0" * 32

        with self.assertRaises(ArtifactNotFoundError):
            await self.services.delivery_service.select_many(
                artifact_ids=[
                    *(item.artifact_id for item in artifacts),
                    unknown,
                ],
                access=self.access,
                client_type="telegram",
            )

        records = await self.services.delivery_store.list_cycle(
            session_id="session-1",
            cycle_id="cycle-1",
        )
        self.assertEqual(records, [])

    async def test_batch_write_failure_rolls_back_every_record(self):
        artifacts = [await self._artifact(f"report {index}") for index in range(3)]
        store = self.services.delivery_store
        original_write = store._write_sync
        calls = 0

        def fail_second_write(record, *, replace):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ArtifactStorageError("simulated batch write failure")
            return original_write(record, replace=replace)

        store._write_sync = Mock(side_effect=fail_second_write)
        with self.assertRaises(ArtifactStorageError):
            await self.services.delivery_service.select_many(
                artifact_ids=[item.artifact_id for item in artifacts],
                access=self.access,
                client_type="telegram",
            )

        records = await store.list_cycle(
            session_id="session-1",
            cycle_id="cycle-1",
        )
        self.assertEqual(records, [])

    async def test_batch_cancel_rejection_keeps_all_selections(self):
        first = await self._artifact("first")
        second = await self._artifact("second")
        unselected = await self._artifact("unselected")
        await self.services.delivery_service.select_many(
            artifact_ids=[first.artifact_id, second.artifact_id],
            access=self.access,
            client_type="web",
        )

        with self.assertRaises(ArtifactDeliveryNotFoundError):
            await self.services.delivery_service.cancel_many_by_artifact_ids(
                artifact_ids=[
                    first.artifact_id,
                    second.artifact_id,
                    unselected.artifact_id,
                ],
                access=self.access,
                client_type="web",
            )

        refs = await self.services.delivery_service.list_cycle_refs(
            session_id="session-1",
            cycle_id="cycle-1",
        )
        self.assertEqual(len(refs), 2)
        self.assertTrue(all(
            item.state == ArtifactDeliveryState.SELECTED for item in refs
        ))


if __name__ == "__main__":
    unittest.main()
