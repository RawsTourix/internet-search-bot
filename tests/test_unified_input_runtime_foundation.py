import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.artifacts import ArtifactConfigType, create_artifact_services
from src.core.message_processor import MessageProcessor
from src.core.models import ClientType, MessageType, UnifiedMessage
from src.ingress import (
    ClientAttachmentLocator,
    ClientConversationRef,
    ClientInputEnvelope,
    ClientResponseRoute,
    ClientSenderRef,
    IngressAttachmentSlot,
    IngressConfigType,
    IngressConflictError,
    IngressTextPart,
    InputGroupingAmbiguityError,
    InputSubmissionResult,
    create_ingress_services,
    legacy_message_to_input_envelope,
)
from src.storage import StorageConfigType, create_storage_services


async def chunks(*values: bytes):
    for value in values:
        yield value


class UnifiedInputRuntimeFoundationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.storage_config = StorageConfigType(
            root_dir=str(self.root / "storage")
        )
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
                media_group_quiet_timeout_seconds=0.12,
                media_group_sealing_grace_seconds=0.0,
                media_group_maximum_wait_seconds=1.0,
            ),
            content_store=self.storage.content_store,
            artifact_services=self.artifacts,
        )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    def _file_envelope(
        self,
        *,
        message_id: str,
        group_id: str,
        sender_id: str = "user-1",
        sender_name: str | None = None,
        filename: str = "source.md",
    ) -> ClientInputEnvelope:
        slot_id = f"slot-{message_id}"
        return ClientInputEnvelope(
            idempotency_key=f"telegram:bot:update:{message_id}",
            client_type=ClientType.TELEGRAM,
            client_instance_id="bot-1",
            conversation=ClientConversationRef(conversation_id="chat-1"),
            sender=ClientSenderRef(
                principal_id=sender_id,
                display_name=sender_name,
            ),
            source_update_id=f"update-{message_id}",
            source_message_id=message_id,
            source_group_id=group_id,
            occurred_at=datetime.now(timezone.utc),
            text_parts=[],
            attachment_slots=[IngressAttachmentSlot(
                slot_id=slot_id,
                media_kind="document",
                original_filename=filename,
                declared_mime_type="text/markdown",
                declared_size_bytes=5,
                transport_locator=ClientAttachmentLocator(
                    provider="telegram",
                    locator=f"file-{message_id}",
                ),
            )],
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="chat-1",
                reply_to_message_id=message_id,
            ),
        )

    def _text_envelope(
        self,
        *,
        message_id: str,
        sender_id: str = "user-1",
        sender_name: str | None = None,
        text: str = "Process all files",
    ) -> ClientInputEnvelope:
        return ClientInputEnvelope(
            idempotency_key=f"telegram:bot:update:{message_id}",
            client_type=ClientType.TELEGRAM,
            client_instance_id="bot-1",
            conversation=ClientConversationRef(conversation_id="chat-1"),
            sender=ClientSenderRef(
                principal_id=sender_id,
                display_name=sender_name,
            ),
            source_update_id=f"update-{message_id}",
            source_message_id=message_id,
            occurred_at=datetime.now(timezone.utc),
            text_parts=[IngressTextPart(
                part_id=f"text-{message_id}",
                kind="message_text",
                text=text,
                attachment_slot_ids=[],
            )],
            attachment_slots=[],
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="chat-1",
                reply_to_message_id=message_id,
            ),
        )

    async def _submit_file(self, envelope: ClientInputEnvelope):
        slot_id = envelope.attachment_slots[0].slot_id
        return await self.ingress.ingress_service.submit_atomic(
            envelope,
            session_id="telegram:conversation:chat-1",
            upload_streams={slot_id: chunks(b"hello")},
        )

    async def test_text_joins_the_only_open_media_group_draft(self):
        first = await self._submit_file(
            self._file_envelope(message_id="1", group_id="album-a")
        )
        self.assertEqual(first.state, "collecting")

        text = await self.ingress.ingress_service.submit_atomic(
            self._text_envelope(message_id="2"),
            session_id="telegram:conversation:chat-1",
        )
        self.assertEqual(text.state, "collecting")
        self.assertEqual(text.input_batch_id, first.input_batch_id)

        batch, duplicate = await self.ingress.batch_store.commit_batch(
            first.input_batch_id,
            session_id="telegram:conversation:chat-1",
            reason="test_commit",
        )
        self.assertFalse(duplicate)
        self.assertEqual(len(batch.artifact_refs), 1)
        self.assertEqual(
            [part.text for part in batch.text_parts],
            ["Process all files"],
        )
        self.assertEqual(len(batch.source_event_ids), 2)

    async def test_ten_file_album_and_instruction_share_one_presentation(self):
        submissions = []
        for index in range(1, 11):
            submissions.append(
                await self._submit_file(
                    self._file_envelope(
                        message_id=str(index),
                        group_id="album-ten",
                        filename=f"part-{index}.md",
                    )
                )
            )
            if index == 1:
                public = submissions[0].presentation_ref
                await self.ingress.presentation_store.bind(
                    public.presentation_id,
                    client_message_id="900",
                    token=public.presentation_token,
                )

        instruction = await self.ingress.ingress_service.submit_atomic(
            self._text_envelope(
                message_id="instruction",
                text="Summarize all ten files",
            ),
            session_id="telegram:conversation:chat-1",
        )
        submissions.append(instruction)

        self.assertEqual(
            {item.input_batch_id for item in submissions},
            {submissions[0].input_batch_id},
        )
        self.assertEqual(
            {
                item.presentation_ref.presentation_id
                for item in submissions
                if item.presentation_ref is not None
            },
            {submissions[0].presentation_ref.presentation_id},
        )
        self.assertEqual(
            sum(item.ack_policy.value == "create" for item in submissions),
            1,
        )
        self.assertEqual(
            instruction.response_anchor.client_message_id,
            "instruction",
        )

        batch, duplicate, presentation_result = (
            await self.ingress.ingress_service.commit_batch_application_result(
                submissions[0].input_batch_id,
                session_id="telegram:conversation:chat-1",
                reason="test_album_complete",
            )
        )
        self.assertFalse(duplicate)
        self.assertEqual(len(batch.artifact_refs), 10)
        self.assertEqual(batch.text_parts[0].text, "Summarize all ten files")
        self.assertIsNotNone(presentation_result)
        stored = await self.ingress.presentation_store.get(
            submissions[0].presentation_ref.presentation_id
        )
        self.assertEqual(stored.state.value, "closed")
        self.assertEqual(stored.client_message_id, "900")

    async def test_client_without_message_edit_uses_silent_existing_ack(self):
        self.ingress.ingress_service.telegram_message_editing = False
        first = await self._submit_file(
            self._file_envelope(message_id="no-edit-1", group_id="no-edit")
        )
        await self.ingress.presentation_store.bind(
            first.presentation_ref.presentation_id,
            client_message_id="901",
            token=first.presentation_ref.presentation_token,
        )

        instruction = await self.ingress.ingress_service.submit_atomic(
            self._text_envelope(
                message_id="no-edit-instruction",
                text="Use the attached file",
            ),
            session_id="telegram:conversation:chat-1",
        )

        self.assertEqual(instruction.input_batch_id, first.input_batch_id)
        self.assertEqual(instruction.ack_policy.value, "silent")
        self.assertEqual(
            instruction.presentation_ref.presentation_id,
            first.presentation_ref.presentation_id,
        )
        stored = await self.ingress.presentation_store.get(
            first.presentation_ref.presentation_id
        )
        self.assertEqual(stored.state.value, "bound")
        self.assertEqual(stored.client_message_id, "901")

    async def test_display_name_change_does_not_change_sender_authority(self):
        first = await self._submit_file(self._file_envelope(
            message_id="1",
            group_id="album-a",
            sender_name="Old Name",
        ))
        text = await self.ingress.ingress_service.submit_atomic(
            self._text_envelope(
                message_id="2",
                sender_name="New Name",
            ),
            session_id="telegram:conversation:chat-1",
        )
        self.assertEqual(text.input_batch_id, first.input_batch_id)

    async def test_different_sender_does_not_join_open_draft(self):
        first = await self._submit_file(
            self._file_envelope(message_id="1", group_id="album-a")
        )
        other = await self.ingress.ingress_service.submit_atomic(
            self._text_envelope(message_id="2", sender_id="user-2"),
            session_id="telegram:conversation:chat-1",
        )
        self.assertEqual(other.state, "committed")
        self.assertNotEqual(other.input_batch_id, first.input_batch_id)
        draft = await self.ingress.batch_store.get_draft(first.input_batch_id)
        self.assertEqual(draft.text_parts, [])

    async def test_ambiguous_open_drafts_are_not_guessed(self):
        await self._submit_file(
            self._file_envelope(message_id="1", group_id="album-a")
        )
        await self._submit_file(
            self._file_envelope(message_id="2", group_id="album-b")
        )
        with self.assertRaises(InputGroupingAmbiguityError):
            await self.ingress.ingress_service.submit_atomic(
                self._text_envelope(message_id="3"),
                session_id="telegram:conversation:chat-1",
            )

    async def test_text_resets_durable_quiet_deadline_before_commit(self):
        first = await self._submit_file(
            self._file_envelope(message_id="1", group_id="album-a")
        )
        commit_task = asyncio.create_task(
            self.ingress.batch_store.commit_batch(
                first.input_batch_id,
                session_id="telegram:conversation:chat-1",
                reason="test_commit",
            )
        )
        await asyncio.sleep(0.03)
        await self.ingress.ingress_service.submit_atomic(
            self._text_envelope(message_id="2"),
            session_id="telegram:conversation:chat-1",
        )
        await asyncio.sleep(0.05)
        self.assertFalse(commit_task.done())
        batch, _ = await asyncio.wait_for(commit_task, timeout=1)
        self.assertEqual(
            [part.text for part in batch.text_parts],
            ["Process all files"],
        )

    async def test_open_draft_limit_conflict_is_not_bypassed_by_atomic_batch(self):
        limited_storage_config = StorageConfigType(
            root_dir=str(self.root / "limited-storage")
        )
        limited_storage = create_storage_services(limited_storage_config)
        limited_artifacts = create_artifact_services(
            storage_config=limited_storage_config,
            artifact_config=ArtifactConfigType(
                max_artifact_size_bytes=1024 * 1024,
                max_patchable_text_bytes=1024 * 1024,
                max_workspace_bytes=2 * 1024 * 1024,
            ),
            content_store=limited_storage.content_store,
        )
        limited = create_ingress_services(
            storage_config=limited_storage_config,
            ingress_config=IngressConfigType(
                max_text_parts_per_batch=1,
                media_group_quiet_timeout_seconds=0.2,
                media_group_sealing_grace_seconds=0.0,
                media_group_maximum_wait_seconds=1.0,
            ),
            content_store=limited_storage.content_store,
            artifact_services=limited_artifacts,
        )
        file_envelope = self._file_envelope(
            message_id="limit-1",
            group_id="limit-album",
        )
        first = await limited.ingress_service.submit_atomic(
            file_envelope,
            session_id="telegram:conversation:chat-1",
            upload_streams={
                file_envelope.attachment_slots[0].slot_id: chunks(b"hello")
            },
        )
        await limited.ingress_service.submit_atomic(
            self._text_envelope(message_id="limit-2", text="First instruction"),
            session_id="telegram:conversation:chat-1",
        )
        with self.assertRaises(IngressConflictError):
            await limited.ingress_service.submit_atomic(
                self._text_envelope(
                    message_id="limit-3",
                    text="Second instruction",
                ),
                session_id="telegram:conversation:chat-1",
            )
        drafts = await limited.batch_store.list_open_drafts(
            session_id="telegram:conversation:chat-1"
        )
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0].input_batch_id, first.input_batch_id)
        self.assertEqual(
            [part.text for part in drafts[0].text_parts],
            ["First instruction"],
        )

    def test_ingress_default_lifetime_covers_long_media_groups(self):
        config = IngressConfigType()
        self.assertEqual(config.media_group_maximum_wait_seconds, 300.0)
        with self.assertRaises(ValueError):
            IngressConfigType(
                media_group_quiet_timeout_seconds=2.0,
                media_group_sealing_grace_seconds=1.0,
                media_group_maximum_wait_seconds=2.5,
            )

    def test_legacy_message_normalizes_to_semantic_envelope(self):
        message = UnifiedMessage(
            id="legacy-1",
            client_type=ClientType.TELEGRAM,
            message_type=MessageType.TEXT,
            content="Process the album",
            user_id="user-1",
            user_name="User",
            timestamp=datetime.now(timezone.utc),
            metadata={
                "chat_id": "chat-1",
                "message_id": "42",
                "session_id": "telegram:conversation:chat-1",
            },
        )
        envelope = legacy_message_to_input_envelope(message)
        self.assertEqual(envelope.conversation.conversation_id, "chat-1")
        self.assertEqual(envelope.sender.principal_id, "user-1")
        self.assertEqual(envelope.text_parts[0].kind, "message_text")
        self.assertEqual(envelope.text_parts[0].text, "Process the album")
        self.assertTrue(envelope.metadata["legacy_compatibility_wrapper"])

    async def test_message_processor_does_not_run_agent_for_collecting_batch(self):
        processor = MessageProcessor()
        message = UnifiedMessage(
            id="legacy-1",
            client_type=ClientType.TELEGRAM,
            message_type=MessageType.TEXT,
            content="Process the album",
            user_id="user-1",
            timestamp=datetime.now(timezone.utc),
            metadata={
                "chat_id": "chat-1",
                "message_id": "42",
                "session_id": "telegram:conversation:chat-1",
            },
        )
        submission = InputSubmissionResult(
            event_id="evt_" + "0" * 32,
            input_batch_id="ibat_" + "0" * 32,
            state="collecting",
        )
        with patch(
            "src.core.message_processor.API.submit_input",
            new=AsyncMock(return_value=submission),
        ), patch(
            "src.core.message_processor.API.call_agent_batch",
            new=AsyncMock(),
        ) as call_agent:
            response = await processor.process_message(message)
        self.assertIn("добавлено к открытому пакету", response.content)
        call_agent.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
