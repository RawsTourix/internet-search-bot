import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from src.api.artifact_transport import ArtifactTransportFacade
from src.artifacts import ArtifactAccessError, ArtifactConfigType, create_artifact_services
from src.core.models import ClientType
from src.ingress import (
    ClientConversationRef,
    ClientInputEnvelope,
    ClientResponseRoute,
    ClientSenderRef,
    IngressAttachmentSlot,
    IngressConfigType,
    IngressConflictError,
    IngressNotFoundError,
    InputGroupingMode,
    create_ingress_services,
)
from src.storage import StorageConfigType, create_storage_services


async def chunks(value: bytes):
    yield value


class ArtifactIngressGroupingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.storage_config = StorageConfigType(root_dir=str(root / "storage"))
        self.storage = create_storage_services(self.storage_config)
        self.artifacts = create_artifact_services(
            storage_config=self.storage_config,
            artifact_config=ArtifactConfigType(
                max_artifact_size_bytes=1024 * 1024,
                max_patchable_text_bytes=1024 * 1024,
                max_workspace_bytes=2 * 1024 * 1024,
            ),
            content_store=self.storage.content_store,
        )
        self.ingress = create_ingress_services(
            storage_config=self.storage_config,
            ingress_config=IngressConfigType(
                max_batch_total_bytes=2 * 1024 * 1024,
                media_group_quiet_timeout_seconds=1,
                media_group_sealing_grace_seconds=1,
                media_group_maximum_wait_seconds=5,
            ),
            content_store=self.storage.content_store,
            artifact_services=self.artifacts,
        )
        self.facade = ArtifactTransportFacade(
            api=SimpleNamespace(
                ingress_services=self.ingress,
                artifact_config=self.artifacts.config,
            ),
            message_processor=SimpleNamespace(),
        )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    def _envelope(self, *, index: int, group_id: str = "album-1"):
        slot_id = f"slot_file_{index}"
        return ClientInputEnvelope(
            idempotency_key=f"telegram:bot-1:update-{index}",
            client_type=ClientType.TELEGRAM,
            client_instance_id="bot-1",
            conversation=ClientConversationRef(conversation_id="chat-1"),
            sender=ClientSenderRef(principal_id="user-1"),
            source_update_id=f"update-{index}",
            source_message_id=f"message-{index}",
            source_group_id=group_id,
            occurred_at=datetime.now(timezone.utc),
            attachment_slots=[IngressAttachmentSlot(
                slot_id=slot_id,
                media_kind="document",
                original_filename=f"part-{index}.md",
                declared_mime_type="text/markdown",
                declared_size_bytes=6,
                upload_field_name=f"file_{index}",
            )],
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="chat-1",
                reply_to_message_id=f"message-{index}",
            ),
        )

    async def _submit_group_part(self, index: int):
        return await self.ingress.ingress_service.submit_atomic(
            self._envelope(index=index),
            session_id="telegram:conversation:chat-1",
            grouping_mode=InputGroupingMode.MEDIA_GROUP,
            grouping_key="bot-1:chat-1:user-1:album-1",
            upload_streams={f"slot_file_{index}": chunks(b"hello\n")},
        )

    async def test_media_group_stays_hidden_until_explicit_commit(self):
        first = await self._submit_group_part(1)
        second = await self._submit_group_part(2)

        self.assertEqual(first.state, "collecting")
        self.assertEqual(second.state, "collecting")
        self.assertEqual(first.input_batch_id, second.input_batch_id)
        with self.assertRaises(IngressNotFoundError):
            await self.ingress.batch_store.get_committed(first.input_batch_id)

        batch, duplicate = await self.facade.commit_grouped_batch(
            first.input_batch_id,
            session_id="telegram:conversation:chat-1",
        )
        self.assertFalse(duplicate)
        self.assertEqual(len(batch.artifact_refs), 2)
        self.assertEqual(len(batch.source_event_ids), 2)

        repeated, duplicate = await self.facade.commit_grouped_batch(
            first.input_batch_id,
            session_id="telegram:conversation:chat-1",
        )
        self.assertTrue(duplicate)
        self.assertEqual(repeated, batch)
        self.assertEqual(
            await self.ingress.batch_store.list_open_drafts(
                session_id="telegram:conversation:chat-1"
            ),
            [],
        )

    async def test_commit_enforces_session_authority(self):
        result = await self._submit_group_part(1)
        with self.assertRaises(ArtifactAccessError):
            await self.facade.commit_grouped_batch(
                result.input_batch_id,
                session_id="telegram:conversation:other-chat",
            )

    async def test_grouped_commit_route_contract_rejects_atomic_batch(self):
        envelope = self._envelope(index=1)
        atomic = await self.ingress.ingress_service.submit_atomic(
            envelope,
            session_id="telegram:conversation:chat-1",
            upload_streams={"slot_file_1": chunks(b"hello\n")},
        )
        with self.assertRaises(IngressConflictError):
            await self.facade.commit_grouped_batch(
                atomic.input_batch_id,
                session_id="telegram:conversation:chat-1",
            )


if __name__ == "__main__":
    unittest.main()
