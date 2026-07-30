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
    InputAttachmentState,
    InputBatchDraftState,
    InputCollectionState,
    InputDraftControlStatus,
    InputDraftScope,
    create_ingress_services,
)
from src.ingress.explicit_policy import (
    EXPLICIT_COLLECTION_GROUPING_MODE,
    EXPLICIT_COLLECTION_ROUTE_METADATA_KEY,
)
from src.ingress.store import IngressConflictError
from src.storage import StorageConfigType, create_storage_services


async def chunks(value: bytes):
    yield value


class ExplicitCollectionGroupingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.storage_config = StorageConfigType(
            root_dir=str(self.root / "storage")
        )
        self.ingress_config = IngressConfigType(
            max_batch_total_bytes=2 * 1024 * 1024,
            media_group_quiet_timeout_seconds=0.01,
            media_group_sealing_grace_seconds=0.0,
            media_group_maximum_wait_seconds=1.0,
        )
        self.ingress = self._create_services()
        self.service = self.ingress.ingress_service
        self.control = self.ingress.draft_control_service
        self.collections = self.ingress.collection_store
        self.session_id = "telegram:conversation:chat-explicit"

    async def asyncTearDown(self):
        self.temporary.cleanup()

    def _create_services(self):
        storage = create_storage_services(self.storage_config)
        artifacts = create_artifact_services(
            storage_config=self.storage_config,
            artifact_config=ArtifactConfigType(
                max_artifact_size_bytes=1024 * 1024,
                max_patchable_text_bytes=1024 * 1024,
                max_workspace_bytes=2 * 1024 * 1024,
            ),
            content_store=storage.content_store,
        )
        return create_ingress_services(
            storage_config=self.storage_config,
            ingress_config=self.ingress_config,
            content_store=storage.content_store,
            artifact_services=artifacts,
        )

    def _scope(self) -> InputDraftScope:
        return InputDraftScope(
            session_id=self.session_id,
            client_type=ClientType.TELEGRAM,
            client_instance_id="bot-1",
            conversation=ClientConversationRef(
                conversation_id="chat-explicit"
            ),
            principal_id="user-1",
        )

    @staticmethod
    def _route(*, metadata=None) -> ClientResponseRoute:
        return ClientResponseRoute(
            route_type="telegram",
            conversation_id="chat-explicit",
            metadata=dict(metadata or {}),
        )

    def _text_envelope(self, suffix: str) -> ClientInputEnvelope:
        return ClientInputEnvelope(
            idempotency_key=f"telegram:bot-1:update:text-{suffix}",
            client_type=ClientType.TELEGRAM,
            client_instance_id="bot-1",
            conversation=ClientConversationRef(
                conversation_id="chat-explicit"
            ),
            sender=ClientSenderRef(principal_id="user-1"),
            source_update_id=f"text-update-{suffix}",
            source_message_id=f"text-message-{suffix}",
            occurred_at=datetime.now(timezone.utc),
            text_parts=[
                IngressTextPart(
                    part_id=f"text-part-{suffix}",
                    kind="message_text",
                    text=f"instruction {suffix}",
                )
            ],
            response_route=self._route(),
        )

    def _file_envelope(self, suffix: str) -> ClientInputEnvelope:
        return ClientInputEnvelope(
            idempotency_key=f"telegram:bot-1:update:file-{suffix}",
            client_type=ClientType.TELEGRAM,
            client_instance_id="bot-1",
            conversation=ClientConversationRef(
                conversation_id="chat-explicit"
            ),
            sender=ClientSenderRef(principal_id="user-1"),
            source_update_id=f"file-update-{suffix}",
            source_message_id=f"file-message-{suffix}",
            source_group_id=f"album-{suffix}",
            occurred_at=datetime.now(timezone.utc),
            attachment_slots=[
                IngressAttachmentSlot(
                    slot_id=f"slot-file-{suffix}",
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

    async def _start_collection(self, key="collect-1"):
        return await self.control.start_collection(
            self._scope(),
            response_route=self._route(),
            locale="ru",
            idempotency_key=key,
        )

    async def test_text_events_remain_collecting_until_explicit_send(self):
        started = await self._start_collection()
        first = await self.service.submit_atomic(
            self._text_envelope("1"),
            session_id=self.session_id,
        )
        second = await self.service.submit_atomic(
            self._text_envelope("2"),
            session_id=self.session_id,
        )

        self.assertEqual(first.state, "collecting")
        self.assertEqual(second.state, "collecting")
        self.assertEqual(first.input_batch_id, second.input_batch_id)
        active = await self.collections.get(started.collection.collection_id)
        self.assertEqual(active.bound_input_batch_id, first.input_batch_id)
        draft = await self.ingress.batch_store.get_draft(first.input_batch_id)
        self.assertEqual(draft.grouping_mode, EXPLICIT_COLLECTION_GROUPING_MODE)
        self.assertEqual(draft.grouping_key, active.collection_id)
        self.assertEqual(len(draft.text_parts), 2)
        self.assertIsNone(draft.quiet_deadline)
        self.assertIsNone(draft.sealing_deadline)
        self.assertIsNone(draft.maximum_deadline)

        committed = await self.control.commit(
            self._scope(),
            idempotency_key="send-1",
        )
        self.assertEqual(
            committed.status,
            InputDraftControlStatus.COMMITTED,
        )
        self.assertEqual(len(committed.committed_batch.text_parts), 2)
        self.assertEqual(committed.committed_batch.artifact_refs, [])

    async def test_mixed_files_and_text_share_one_explicit_batch(self):
        await self._start_collection()
        first_file = await self.service.submit_atomic(
            self._file_envelope("1"),
            session_id=self.session_id,
            upload_streams={"slot-file-1": chunks(b"hello")},
        )
        second_file = await self.service.submit_atomic(
            self._file_envelope("2"),
            session_id=self.session_id,
            upload_streams={"slot-file-2": chunks(b"world")},
        )
        text = await self.service.submit_atomic(
            self._text_envelope("1"),
            session_id=self.session_id,
        )

        self.assertEqual(first_file.input_batch_id, second_file.input_batch_id)
        self.assertEqual(first_file.input_batch_id, text.input_batch_id)
        draft = await self.ingress.batch_store.get_draft(
            first_file.input_batch_id
        )
        self.assertEqual(len(draft.attachment_parts), 2)
        self.assertEqual(len(draft.text_parts), 1)
        self.assertTrue(
            all(
                item.state == InputAttachmentState.STORED
                for item in draft.attachment_parts
            )
        )
        self.assertEqual(draft.grouping_mode, EXPLICIT_COLLECTION_GROUPING_MODE)
        self.assertNotIn(
            draft.input_batch_id,
            {
                item.input_batch_id
                for item in await self.ingress.batch_store.list_ready_drafts()
            },
        )

        committed = await self.control.commit(
            self._scope(),
            idempotency_key="send-1",
        )
        self.assertEqual(len(committed.committed_batch.artifact_refs), 2)
        self.assertEqual(len(committed.committed_batch.text_parts), 1)

    async def test_files_first_promotion_clears_transport_deadlines_and_guard(self):
        file_result = await self.service.submit_atomic(
            self._file_envelope("1"),
            session_id=self.session_id,
            upload_streams={"slot-file-1": chunks(b"hello")},
        )
        before = await self.ingress.batch_store.get_draft(
            file_result.input_batch_id
        )
        self.assertIsNotNone(before.quiet_deadline)

        started = await self._start_collection()
        self.assertEqual(
            started.status,
            InputDraftControlStatus.PROMOTED_AUTO_DRAFT,
        )
        promoted = await self.ingress.batch_store.get_draft(
            file_result.input_batch_id
        )
        self.assertEqual(
            promoted.grouping_mode,
            EXPLICIT_COLLECTION_GROUPING_MODE,
        )
        self.assertIsNone(promoted.quiet_deadline)
        self.assertIsNone(promoted.sealing_deadline)
        self.assertIsNone(promoted.maximum_deadline)

        with self.assertRaises(IngressConflictError):
            await self.ingress.batch_store.commit_batch(
                promoted.input_batch_id,
                session_id=self.session_id,
                reason="telegram_media_group_complete",
            )
        current = await self.ingress.batch_store.get_draft(
            promoted.input_batch_id
        )
        self.assertEqual(current.state, InputBatchDraftState.COLLECTING)

        committed = await self.control.commit(
            self._scope(),
            idempotency_key="send-1",
        )
        self.assertEqual(
            committed.collection.state,
            InputCollectionState.COMMITTED,
        )

    async def test_restart_preserves_active_explicit_collection(self):
        started = await self._start_collection()
        submitted = await self.service.submit_atomic(
            self._text_envelope("1"),
            session_id=self.session_id,
        )

        restarted = self._create_services()
        await restarted.ingress_service.commit_ready_drafts()
        active = await restarted.collection_store.get_active(self._scope())
        self.assertIsNotNone(active)
        self.assertEqual(active.collection_id, started.collection.collection_id)
        self.assertEqual(active.bound_input_batch_id, submitted.input_batch_id)
        draft = await restarted.batch_store.get_draft(submitted.input_batch_id)
        self.assertEqual(draft.state, InputBatchDraftState.COLLECTING)
        self.assertEqual(draft.grouping_mode, EXPLICIT_COLLECTION_GROUPING_MODE)

        committed = await restarted.draft_control_service.commit(
            self._scope(),
            idempotency_key="send-after-restart",
        )
        self.assertEqual(
            committed.status,
            InputDraftControlStatus.COMMITTED,
        )

    async def test_client_cannot_spoof_server_owned_collection_route(self):
        fake_collection_id = "icol_" + "0" * 32
        envelope = self._text_envelope("spoof")
        envelope = envelope.model_copy(
            update={
                "response_route": self._route(
                    metadata={
                        EXPLICIT_COLLECTION_ROUTE_METADATA_KEY: fake_collection_id
                    }
                )
            }
        )
        result = await self.service.submit_atomic(
            envelope,
            session_id=self.session_id,
        )

        self.assertEqual(result.state, "committed")
        self.assertIsNotNone(result.committed_batch)
        draft = await self.ingress.batch_store.get_draft(result.input_batch_id)
        self.assertNotEqual(
            draft.grouping_mode,
            EXPLICIT_COLLECTION_GROUPING_MODE,
        )


if __name__ == "__main__":
    unittest.main()
