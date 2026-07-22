import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from src.api.artifact_transport import (
    ArtifactTransportFacade,
    AttachmentProviderError,
)
from src.artifacts import ArtifactConfigType, create_artifact_services
from src.core.models import ClientType
from src.ingress import (
    ClientAttachmentLocator,
    ClientConversationRef,
    ClientInputEnvelope,
    ClientResponseRoute,
    ClientSenderRef,
    IngressAttachmentSlot,
    IngressConfigType,
    InputBatchDraftState,
    create_ingress_services,
)
from src.storage import StorageConfigType, create_storage_services


class FailingProvider:
    async def open_stream(self, locator, *, max_size_bytes):
        async def iterator():
            yield b"partial"
            raise AttachmentProviderError("provider interrupted")
        return iterator()


class ArtifactTransportFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        storage_config = StorageConfigType(root_dir=str(root / "storage"))
        storage = create_storage_services(storage_config)
        artifacts = create_artifact_services(
            storage_config=storage_config,
            artifact_config=ArtifactConfigType(
                max_artifact_size_bytes=1024 * 1024,
                max_patchable_text_bytes=1024 * 1024,
                max_workspace_bytes=2 * 1024 * 1024,
            ),
            content_store=storage.content_store,
        )
        ingress = create_ingress_services(
            storage_config=storage_config,
            ingress_config=IngressConfigType(
                max_batch_total_bytes=2 * 1024 * 1024,
            ),
            content_store=storage.content_store,
            artifact_services=artifacts,
        )
        self.api = SimpleNamespace(
            ingress_services=ingress,
            artifact_config=artifacts.config,
        )
        self.facade = ArtifactTransportFacade(
            api=self.api,
            message_processor=SimpleNamespace(),
            providers={"telegram": FailingProvider()},
        )
        self.envelope = ClientInputEnvelope(
            idempotency_key="telegram:bot-1:update:1:message:10",
            client_type=ClientType.TELEGRAM,
            client_instance_id="bot-1",
            conversation=ClientConversationRef(conversation_id="chat-1"),
            sender=ClientSenderRef(principal_id="user-1"),
            source_update_id="1",
            source_message_id="10",
            occurred_at=datetime.now(timezone.utc),
            attachment_slots=[IngressAttachmentSlot(
                slot_id="slot_10-1",
                media_kind="document",
                original_filename="input.txt",
                declared_mime_type="text/plain",
                transport_locator=ClientAttachmentLocator(
                    provider="telegram",
                    locator="file-id",
                ),
            )],
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="chat-1",
                reply_to_message_id="10",
            ),
        )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_provider_failure_closes_partial_draft(self):
        with self.assertRaises(AttachmentProviderError):
            await self.facade.submit_envelope(self.envelope)

        event, duplicate = await self.api.ingress_services.event_store.save_if_absent(
            self.envelope
        )
        self.assertTrue(duplicate)
        draft, committed = await self.api.ingress_services.batch_store.find_by_event(
            event.event_id
        )
        self.assertIsNotNone(draft)
        self.assertIsNone(committed)
        self.assertEqual(draft.state, InputBatchDraftState.FAILED)
        self.assertEqual(draft.failure_code, "attachment_stream_failed")


if __name__ == "__main__":
    unittest.main()
