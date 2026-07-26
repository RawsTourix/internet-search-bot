import tempfile
import unittest

from src.artifacts import (
    ArtifactAccessContext,
    ArtifactConfigType,
    ArtifactProvenance,
    ArtifactPurpose,
    create_artifact_services,
)
from src.artifacts.errors import ArtifactDeliveryError
from src.artifacts.models import ArtifactDeliveryState
from src.storage import create_storage_services
from src.storage.config import StorageConfigType


class AdvancedArtifactDeliveryPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        config = StorageConfigType(root_dir=self.temporary.name)
        storage = create_storage_services(config)
        self.artifacts = create_artifact_services(
            storage_config=config,
            artifact_config=ArtifactConfigType(),
            content_store=storage.content_store,
        )
        self.provenance = ArtifactProvenance(
            origin="agent_created",
            creator="agent",
            operation="delivery_policy_test",
        )
        self.first = await self.artifacts.artifact_service.create_text(
            session_id="session-1",
            cycle_id="cycle-1",
            filename="result.md",
            text="first",
            format_id="markdown",
            purpose=ArtifactPurpose.DELIVERABLE,
            provenance=self.provenance,
        )
        self.access = ArtifactAccessContext(
            session_id="session-1",
            cycle_id="cycle-1",
            allowed_artifact_ids=[self.first.artifact_id],
        )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_selected_head_is_atomically_replaced_with_same_position(self):
        old = await self._select(self.first.artifact_id)
        successor = await self._successor("second")
        new = await self._select(successor.artifact_id)

        stored_old = await self.artifacts.delivery_store.get(old.delivery_id)
        stored_new = await self.artifacts.delivery_store.get(new.delivery_id)
        self.assertEqual(stored_old.state, ArtifactDeliveryState.CANCELLED)
        self.assertEqual(stored_new.state, ArtifactDeliveryState.SELECTED)
        self.assertEqual(stored_new.selection_index, stored_old.selection_index)

    async def test_confirmed_failed_head_can_be_replaced(self):
        old = await self._select(self.first.artifact_id)
        await self.artifacts.delivery_service.claim(old.delivery_id)
        await self.artifacts.delivery_service.fail(
            old.delivery_id,
            error="confirmed transport rejection",
            ambiguous=False,
        )
        successor = await self._successor("second")
        new = await self._select(successor.artifact_id)

        stored_old = await self.artifacts.delivery_store.get(old.delivery_id)
        stored_new = await self.artifacts.delivery_store.get(new.delivery_id)
        self.assertEqual(stored_old.state, ArtifactDeliveryState.CANCELLED)
        self.assertEqual(stored_new.selection_index, stored_old.selection_index)

    async def test_unknown_head_blocks_replacement_and_normal_retry(self):
        old = await self._select(self.first.artifact_id)
        await self.artifacts.delivery_service.claim(old.delivery_id)
        await self.artifacts.delivery_service.fail(
            old.delivery_id,
            error="timeout after send",
            ambiguous=True,
        )
        successor = await self._successor("second")

        with self.assertRaises(ArtifactDeliveryError):
            await self._select(successor.artifact_id)
        with self.assertRaises(ArtifactDeliveryError):
            await self.artifacts.delivery_service.claim(old.delivery_id)
        self.assertEqual(
            (await self.artifacts.delivery_store.get(old.delivery_id)).state,
            ArtifactDeliveryState.UNKNOWN,
        )

    async def _successor(self, text: str):
        return await self.artifacts.artifact_service.replace_text(
            artifact_id=self.first.artifact_id,
            expected_current_artifact_id=self.first.artifact_id,
            access=self.access,
            cycle_id="cycle-1",
            new_text=text,
            provenance=self.provenance,
        )

    async def _select(self, artifact_id: str):
        return await self.artifacts.delivery_service.select(
            artifact_id=artifact_id,
            access=self.access,
            client_type="telegram",
        )


if __name__ == "__main__":
    unittest.main()
