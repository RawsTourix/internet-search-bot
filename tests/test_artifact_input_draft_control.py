import asyncio
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
    InputBatchDraftState,
    InputCollectionState,
    InputDraftControlConflictError,
    InputDraftControlStatus,
    InputDraftScope,
    create_ingress_services,
)
from src.storage import StorageConfigType, create_storage_services


async def chunks(value: bytes):
    yield value


class InputDraftControlTests(unittest.IsolatedAsyncioTestCase):
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
        self.ingress = create_ingress_services(
            storage_config=storage_config,
            ingress_config=IngressConfigType(
                max_batch_total_bytes=2 * 1024 * 1024,
                media_group_quiet_timeout_seconds=0.01,
                media_group_sealing_grace_seconds=0.0,
                media_group_maximum_wait_seconds=1.0,
            ),
            content_store=storage.content_store,
            artifact_services=artifacts,
        )
        self.service = self.ingress.ingress_service
        self.control = self.ingress.draft_control_service
        self.collections = self.ingress.collection_store
        self.assertIsNotNone(self.control)
        self.assertIsNotNone(self.collections)

    async def asyncTearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _scope(
        *,
        principal_id: str = "user-1",
        client_instance_id: str = "bot-1",
    ) -> InputDraftScope:
        return InputDraftScope(
            session_id="telegram:conversation:chat-1",
            client_type=ClientType.TELEGRAM,
            client_instance_id=client_instance_id,
            conversation=ClientConversationRef(conversation_id="chat-1"),
            principal_id=principal_id,
        )

    @staticmethod
    def _route() -> ClientResponseRoute:
        return ClientResponseRoute(
            route_type="telegram",
            conversation_id="chat-1",
        )

    @staticmethod
    def _file_envelope(
        *,
        suffix: str = "1",
        principal_id: str = "user-1",
        client_instance_id: str = "bot-1",
    ) -> ClientInputEnvelope:
        return ClientInputEnvelope(
            idempotency_key=f"telegram:{client_instance_id}:update:file-{suffix}",
            client_type=ClientType.TELEGRAM,
            client_instance_id=client_instance_id,
            conversation=ClientConversationRef(conversation_id="chat-1"),
            sender=ClientSenderRef(principal_id=principal_id),
            source_update_id=f"file-update-{suffix}",
            source_message_id=f"file-message-{suffix}",
            source_group_id=f"album-{suffix}",
            occurred_at=datetime.now(timezone.utc),
            attachment_slots=[IngressAttachmentSlot(
                slot_id=f"slot-file-{suffix}",
                media_kind="document",
                original_filename=f"source-{suffix}.md",
                declared_mime_type="text/markdown",
                declared_size_bytes=5,
                transport_locator=ClientAttachmentLocator(
                    provider="telegram",
                    locator=f"telegram-file-{suffix}",
                ),
            )],
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="chat-1",
                reply_to_message_id=f"file-message-{suffix}",
            ),
        )

    async def _submit_file(
        self,
        *,
        suffix: str = "1",
        principal_id: str = "user-1",
        client_instance_id: str = "bot-1",
    ):
        envelope = self._file_envelope(
            suffix=suffix,
            principal_id=principal_id,
            client_instance_id=client_instance_id,
        )
        return await self.service.submit_atomic(
            envelope,
            session_id="telegram:conversation:chat-1",
            upload_streams={f"slot-file-{suffix}": chunks(b"hello")},
        )

    async def test_start_creates_empty_explicit_collection(self):
        result = await self.control.start_collection(
            self._scope(),
            response_route=self._route(),
            locale="ru",
            idempotency_key="collect-1",
        )

        self.assertEqual(result.status, InputDraftControlStatus.STARTED)
        self.assertFalse(result.duplicate)
        self.assertIsNotNone(result.collection)
        self.assertEqual(result.file_count, 0)
        self.assertEqual(result.text_part_count, 0)
        self.assertIsNone(result.input_batch_id)
        self.assertEqual(
            result.collection.state,
            InputCollectionState.COLLECTING,
        )

    async def test_start_is_idempotent_and_scope_has_one_active_collection(self):
        first = await self.control.start_collection(
            self._scope(),
            response_route=self._route(),
            locale="ru",
            idempotency_key="collect-1",
        )
        retry = await self.control.start_collection(
            self._scope(),
            response_route=self._route(),
            locale="ru",
            idempotency_key="collect-1",
        )
        another_key = await self.control.start_collection(
            self._scope(),
            response_route=self._route(),
            locale="ru",
            idempotency_key="collect-2",
        )

        self.assertTrue(retry.duplicate)
        self.assertEqual(
            retry.collection.collection_id,
            first.collection.collection_id,
        )
        self.assertEqual(
            another_key.status,
            InputDraftControlStatus.ALREADY_ACTIVE,
        )
        self.assertEqual(
            another_key.collection.collection_id,
            first.collection.collection_id,
        )

    async def test_empty_send_does_not_commit_or_close_collection(self):
        started = await self.control.start_collection(
            self._scope(),
            response_route=self._route(),
            locale="ru",
            idempotency_key="collect-1",
        )
        result = await self.control.commit(
            self._scope(),
            idempotency_key="send-1",
        )

        self.assertEqual(result.status, InputDraftControlStatus.EMPTY)
        current = await self.collections.get(started.collection.collection_id)
        self.assertTrue(current.is_active)
        self.assertEqual(current.state, InputCollectionState.COLLECTING)

    async def test_files_first_draft_is_promoted_and_files_only_send_commits(self):
        file_result = await self._submit_file()
        started = await self.control.start_collection(
            self._scope(),
            response_route=self._route(),
            locale="ru",
            idempotency_key="collect-1",
        )

        self.assertEqual(
            started.status,
            InputDraftControlStatus.PROMOTED_AUTO_DRAFT,
        )
        self.assertEqual(started.input_batch_id, file_result.input_batch_id)
        self.assertEqual(started.file_count, 1)
        self.assertEqual(started.text_part_count, 0)

        committed = await self.control.commit(
            self._scope(),
            idempotency_key="send-1",
        )
        self.assertEqual(
            committed.status,
            InputDraftControlStatus.COMMITTED,
        )
        self.assertEqual(len(committed.committed_batch.artifact_refs), 1)
        self.assertEqual(committed.committed_batch.text_parts, [])
        self.assertEqual(
            committed.collection.state,
            InputCollectionState.COMMITTED,
        )

    async def test_commit_request_is_persisted_while_upload_is_in_flight(self):
        stream_started = asyncio.Event()
        release_stream = asyncio.Event()

        async def blocked_stream():
            stream_started.set()
            await release_stream.wait()
            yield b"hello"

        envelope = self._file_envelope()
        task = asyncio.create_task(
            self.service.submit_atomic(
                envelope,
                session_id="telegram:conversation:chat-1",
                upload_streams={"slot-file-1": blocked_stream()},
            )
        )
        try:
            await asyncio.wait_for(stream_started.wait(), timeout=1)
            started = await self.control.start_collection(
                self._scope(),
                response_route=self._route(),
                locale="ru",
                idempotency_key="collect-1",
            )
            self.assertEqual(started.file_count, 1)

            pending = await self.control.commit(
                self._scope(),
                idempotency_key="send-1",
            )
            self.assertEqual(
                pending.status,
                InputDraftControlStatus.COMMIT_REQUESTED,
            )
            self.assertEqual(
                pending.collection.state,
                InputCollectionState.COMMIT_REQUESTED,
            )

            release_stream.set()
            await asyncio.wait_for(task, timeout=1)
            committed = await self.control.commit(
                self._scope(),
                idempotency_key="send-2",
            )
            self.assertEqual(
                committed.status,
                InputDraftControlStatus.COMMITTED,
            )
        finally:
            release_stream.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_cancel_is_exact_and_does_not_cancel_neighbor_scope(self):
        first = await self._submit_file(suffix="1", principal_id="user-1")
        second = await self._submit_file(suffix="2", principal_id="user-2")
        await self.control.start_collection(
            self._scope(principal_id="user-1"),
            response_route=self._route(),
            locale="ru",
            idempotency_key="collect-1",
        )

        cancelled = await self.control.cancel(
            self._scope(principal_id="user-1"),
            idempotency_key="cancel-1",
        )
        self.assertEqual(
            cancelled.status,
            InputDraftControlStatus.CANCELLED,
        )
        first_draft = await self.ingress.batch_store.get_draft(
            first.input_batch_id
        )
        second_draft = await self.ingress.batch_store.get_draft(
            second.input_batch_id
        )
        self.assertEqual(first_draft.state, InputBatchDraftState.CANCELLED)
        self.assertEqual(second_draft.state, InputBatchDraftState.COLLECTING)

    async def test_client_instance_is_part_of_exact_scope(self):
        file_result = await self._submit_file(client_instance_id="bot-1")
        started = await self.control.start_collection(
            self._scope(client_instance_id="bot-2"),
            response_route=self._route(),
            locale="ru",
            idempotency_key="collect-bot-2",
        )

        self.assertEqual(started.status, InputDraftControlStatus.STARTED)
        self.assertIsNone(started.input_batch_id)
        draft = await self.ingress.batch_store.get_draft(
            file_result.input_batch_id
        )
        self.assertEqual(draft.state, InputBatchDraftState.COLLECTING)

    async def test_control_idempotency_key_reuse_with_other_action_is_rejected(self):
        await self.control.start_collection(
            self._scope(),
            response_route=self._route(),
            locale="ru",
            idempotency_key="same-key",
        )
        with self.assertRaises(InputDraftControlConflictError):
            await self.control.cancel(
                self._scope(),
                idempotency_key="same-key",
            )


if __name__ == "__main__":
    unittest.main()
