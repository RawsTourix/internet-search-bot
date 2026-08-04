import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

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
    IngressTextPart,
    InputDraftControlStatus,
    InputDraftScope,
    create_ingress_services,
)
from src.storage import StorageConfigType, create_storage_services


async def chunks(value: bytes):
    yield value


class ExplicitCollectionMultiGroupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.storage_config = StorageConfigType(root_dir=str(root / "storage"))
        storage = create_storage_services(self.storage_config)
        artifacts = create_artifact_services(
            storage_config=self.storage_config,
            artifact_config=ArtifactConfigType(
                max_artifacts_per_cycle=32,
                max_artifact_size_bytes=1024 * 1024,
                max_patchable_text_bytes=1024 * 1024,
                max_workspace_bytes=4 * 1024 * 1024,
            ),
            content_store=storage.content_store,
        )
        self.ingress = create_ingress_services(
            storage_config=self.storage_config,
            ingress_config=IngressConfigType(
                max_events_per_batch=64,
                max_attachments_per_batch=32,
                max_text_parts_per_batch=16,
                max_batch_total_bytes=4 * 1024 * 1024,
            ),
            content_store=storage.content_store,
            artifact_services=artifacts,
        )
        self.session_id = "telegram:conversation:chat-multi-group"
        self.scope = InputDraftScope(
            session_id=self.session_id,
            client_type=ClientType.TELEGRAM,
            client_instance_id="bot-1",
            conversation=ClientConversationRef(
                conversation_id="chat-multi-group"
            ),
            principal_id="user-1",
        )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _route() -> ClientResponseRoute:
        return ClientResponseRoute(
            route_type="telegram",
            conversation_id="chat-multi-group",
        )

    def _file_envelope(
        self,
        *,
        group_number: int,
        item_number: int,
    ) -> ClientInputEnvelope:
        suffix = f"{group_number}-{item_number}"
        return ClientInputEnvelope(
            idempotency_key=f"telegram:bot-1:update:file-{suffix}",
            client_type=ClientType.TELEGRAM,
            client_instance_id="bot-1",
            conversation=ClientConversationRef(
                conversation_id="chat-multi-group"
            ),
            sender=ClientSenderRef(principal_id="user-1"),
            source_update_id=f"file-update-{suffix}",
            source_message_id=f"file-message-{suffix}",
            source_group_id=f"album-{group_number}",
            occurred_at=datetime.now(timezone.utc),
            attachment_slots=[
                IngressAttachmentSlot(
                    slot_id=f"slot-{suffix}",
                    media_kind="document",
                    original_filename=f"source-{suffix}.md",
                    declared_mime_type="text/markdown",
                    declared_size_bytes=5,
                    transport_locator=ClientAttachmentLocator(
                        provider="telegram",
                        locator=f"telegram-file-{suffix}",
                    ),
                )
            ],
            response_route=self._route(),
        )

    def _text_envelope(self, number: int) -> ClientInputEnvelope:
        return ClientInputEnvelope(
            idempotency_key=f"telegram:bot-1:update:text-{number}",
            client_type=ClientType.TELEGRAM,
            client_instance_id="bot-1",
            conversation=ClientConversationRef(
                conversation_id="chat-multi-group"
            ),
            sender=ClientSenderRef(principal_id="user-1"),
            source_update_id=f"text-update-{number}",
            source_message_id=f"text-message-{number}",
            occurred_at=datetime.now(timezone.utc),
            text_parts=[
                IngressTextPart(
                    part_id=f"text-part-{number}",
                    kind="message_text",
                    text=f"instruction {number}",
                )
            ],
            response_route=self._route(),
        )

    async def test_live_shaped_batch_commits_as_30_files_and_7_messages(self):
        started = await self.ingress.draft_control_service.start_collection(
            self.scope,
            response_route=self._route(),
            locale="ru",
            idempotency_key="collect-multi-group",
        )
        self.assertEqual(str(started.status.value), "started")

        batch_id = None
        for group_number in (1, 2, 3, 4):
            for item_number in range(1, 8):
                envelope = self._file_envelope(
                    group_number=group_number,
                    item_number=item_number,
                )
                slot_id = envelope.attachment_slots[0].slot_id
                result = await self.ingress.ingress_service.submit_atomic(
                    envelope,
                    session_id=self.session_id,
                    upload_streams={slot_id: chunks(b"hello")},
                )
                batch_id = batch_id or result.input_batch_id
                self.assertEqual(result.input_batch_id, batch_id)
                self.assertEqual(result.state, "collecting")

        for item_number in (1, 2):
            extra = self._file_envelope(
                group_number=5,
                item_number=item_number,
            )
            extra_slot_id = extra.attachment_slots[0].slot_id
            extra_result = await self.ingress.ingress_service.submit_atomic(
                extra,
                session_id=self.session_id,
                upload_streams={extra_slot_id: chunks(b"extra")},
            )
            self.assertEqual(extra_result.input_batch_id, batch_id)

        for number in range(1, 8):
            text_result = await self.ingress.ingress_service.submit_atomic(
                self._text_envelope(number),
                session_id=self.session_id,
            )
            self.assertEqual(text_result.input_batch_id, batch_id)

        draft = await self.ingress.batch_store.get_draft(batch_id)
        self.assertEqual(len(draft.attachment_parts), 30)
        self.assertEqual(len(draft.text_parts), 7)

        committed = await self.ingress.draft_control_service.commit(
            self.scope,
            idempotency_key="send-multi-group",
        )
        self.assertEqual(
            committed.status,
            InputDraftControlStatus.COMMITTED,
        )
        self.assertEqual(len(committed.committed_batch.artifact_refs), 30)
        self.assertEqual(len(committed.committed_batch.text_parts), 7)
        self.assertEqual(committed.file_count, 30)
        self.assertEqual(committed.text_part_count, 7)


if __name__ == "__main__":
    unittest.main()
