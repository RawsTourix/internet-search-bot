import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.artifacts import (
    ArtifactConfigType,
    ArtifactLimitError,
    create_artifact_services,
)
from src.core.models import ClientType
from src.ingress import (
    ClientAttachmentLocator,
    ClientConversationRef,
    ClientInputEnvelope,
    ClientResponseRoute,
    ClientSenderRef,
    IngressAttachmentSlot,
    IngressConfigType,
    InputDraftControlStatus,
    InputDraftScope,
    create_ingress_services,
)
from src.storage import StorageConfigType, create_storage_services


async def chunks(value: bytes):
    yield value


class ExplicitCollectionRejectionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.storage_config = StorageConfigType(
            root_dir=str(root / "storage")
        )
        storage = create_storage_services(self.storage_config)
        artifacts = create_artifact_services(
            storage_config=self.storage_config,
            artifact_config=ArtifactConfigType(
                max_artifacts_per_cycle=8,
                max_artifact_size_bytes=1024 * 1024,
                max_patchable_text_bytes=1024 * 1024,
                max_workspace_bytes=4 * 1024 * 1024,
            ),
            content_store=storage.content_store,
        )
        self.ingress = create_ingress_services(
            storage_config=self.storage_config,
            ingress_config=IngressConfigType(
                max_attachments_per_batch=2,
                max_batch_total_bytes=4 * 1024 * 1024,
                explicit_collection_idle_timeout_seconds=60.0,
            ),
            content_store=storage.content_store,
            artifact_services=artifacts,
        )
        self.scope = InputDraftScope(
            session_id="telegram:conversation:limit-chat",
            client_type=ClientType.TELEGRAM,
            client_instance_id="bot-1",
            conversation=ClientConversationRef(
                conversation_id="limit-chat"
            ),
            principal_id="user-1",
        )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _route() -> ClientResponseRoute:
        return ClientResponseRoute(
            route_type="telegram",
            conversation_id="limit-chat",
        )

    def _file_envelope(self, number: int) -> ClientInputEnvelope:
        return ClientInputEnvelope(
            idempotency_key=f"telegram:bot-1:update:file-{number}",
            client_type=ClientType.TELEGRAM,
            client_instance_id="bot-1",
            conversation=ClientConversationRef(
                conversation_id="limit-chat"
            ),
            sender=ClientSenderRef(principal_id="user-1"),
            source_update_id=f"file-update-{number}",
            source_message_id=f"file-message-{number}",
            source_group_id="album-limit",
            occurred_at=datetime.now(timezone.utc),
            attachment_slots=[
                IngressAttachmentSlot(
                    slot_id=f"slot-{number}",
                    media_kind="document",
                    original_filename=f"file-{number}.md",
                    declared_mime_type="text/markdown",
                    declared_size_bytes=5,
                    transport_locator=ClientAttachmentLocator(
                        provider="telegram",
                        locator=f"telegram-file-{number}",
                    ),
                )
            ],
            response_route=self._route(),
        )

    async def _submit(self, number: int):
        envelope = self._file_envelope(number)
        return await self.ingress.ingress_service.submit_atomic(
            envelope,
            session_id=self.scope.session_id,
            upload_streams={
                envelope.attachment_slots[0].slot_id: chunks(b"hello")
            },
        )

    async def test_limit_rejection_does_not_fail_or_escape_collection(self):
        await self.ingress.draft_control_service.start_collection(
            self.scope,
            response_route=self._route(),
            locale="ru",
            idempotency_key="collect-limit",
        )
        first = await self._submit(1)
        second = await self._submit(2)
        self.assertEqual(first.input_batch_id, second.input_batch_id)

        with self.assertRaises(ArtifactLimitError):
            await self._submit(3)

        inspected = await self.ingress.draft_control_service.inspect(self.scope)
        open_drafts = await self.ingress.batch_store.list_open_drafts(
            session_id=self.scope.session_id
        )
        cancelled = await self.ingress.draft_control_service.cancel(
            self.scope,
            idempotency_key="cancel-limit",
        )

        self.assertEqual(inspected.status, InputDraftControlStatus.INSPECTED)
        self.assertEqual(inspected.file_count, 2)
        self.assertEqual(inspected.collection.state.value, "collecting")
        self.assertEqual(len(open_drafts), 1)
        self.assertEqual(open_drafts[0].input_batch_id, first.input_batch_id)
        self.assertEqual(cancelled.status, InputDraftControlStatus.CANCELLED)


if __name__ == "__main__":
    unittest.main()
