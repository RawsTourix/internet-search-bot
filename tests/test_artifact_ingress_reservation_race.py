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
    IngressTextPart,
    create_ingress_services,
)
from src.storage import StorageConfigType, create_storage_services


async def chunks(value: bytes):
    yield value


class IngressReservationRaceTests(unittest.IsolatedAsyncioTestCase):
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
                media_group_quiet_timeout_seconds=0.02,
                media_group_sealing_grace_seconds=0.0,
                media_group_maximum_wait_seconds=1.0,
            ),
            content_store=storage.content_store,
            artifact_services=artifacts,
        )
        self.service = self.ingress.ingress_service
        self.session_id = "telegram:conversation:chat-1"

    async def asyncTearDown(self):
        self.temporary.cleanup()

    def _file_envelope(self) -> ClientInputEnvelope:
        return ClientInputEnvelope(
            idempotency_key="telegram:bot-1:update:file-1",
            client_type=ClientType.TELEGRAM,
            client_instance_id="bot-1",
            conversation=ClientConversationRef(conversation_id="chat-1"),
            sender=ClientSenderRef(principal_id="user-1"),
            source_update_id="file-update-1",
            source_message_id="file-message-1",
            source_group_id="album-1",
            occurred_at=datetime.now(timezone.utc),
            attachment_slots=[IngressAttachmentSlot(
                slot_id="slot-file-1",
                media_kind="document",
                original_filename="source.md",
                declared_mime_type="text/markdown",
                declared_size_bytes=5,
                transport_locator=ClientAttachmentLocator(
                    provider="telegram",
                    locator="telegram-file-1",
                ),
            )],
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="chat-1",
                reply_to_message_id="file-message-1",
            ),
        )

    def _instruction_envelope(self) -> ClientInputEnvelope:
        return ClientInputEnvelope(
            idempotency_key="telegram:bot-1:update:instruction-1",
            client_type=ClientType.TELEGRAM,
            client_instance_id="bot-1",
            conversation=ClientConversationRef(conversation_id="chat-1"),
            sender=ClientSenderRef(principal_id="user-1"),
            source_update_id="instruction-update-1",
            source_message_id="instruction-message-1",
            occurred_at=datetime.now(timezone.utc),
            text_parts=[IngressTextPart(
                part_id="text-instruction-1",
                kind="message_text",
                text="Process the attached files",
                attachment_slot_ids=[],
            )],
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="chat-1",
                reply_to_message_id="instruction-message-1",
            ),
        )

    async def test_instruction_waits_for_file_draft_reservation(self):
        file_envelope = self._file_envelope()
        instruction_envelope = self._instruction_envelope()
        file_reservation_entered = asyncio.Event()
        release_file_reservation = asyncio.Event()
        instruction_persisted = asyncio.Event()

        original_resolve = self.service._resolve_interaction
        original_save = self.service.event_store.save_if_absent

        async def delayed_resolve(envelope):
            if envelope.attachment_slots:
                file_reservation_entered.set()
                await release_file_reservation.wait()
            return await original_resolve(envelope)

        async def tracked_save(envelope, **kwargs):
            result = await original_save(envelope, **kwargs)
            if envelope.source_message_id == "instruction-message-1":
                instruction_persisted.set()
            return result

        self.service._resolve_interaction = delayed_resolve
        self.service.event_store.save_if_absent = tracked_save
        file_task = None
        instruction_task = None
        try:
            file_task = asyncio.create_task(
                self.service.submit_atomic(
                    file_envelope,
                    session_id=self.session_id,
                    upload_streams={"slot-file-1": chunks(b"hello")},
                )
            )
            await asyncio.wait_for(file_reservation_entered.wait(), timeout=1)

            instruction_task = asyncio.create_task(
                self.service.submit_atomic(
                    instruction_envelope,
                    session_id=self.session_id,
                )
            )

            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(
                    instruction_persisted.wait(),
                    timeout=0.1,
                )

            release_file_reservation.set()
            file_result, instruction_result = await asyncio.gather(
                file_task,
                instruction_task,
            )
        finally:
            release_file_reservation.set()
            self.service._resolve_interaction = original_resolve
            self.service.event_store.save_if_absent = original_save
            for task in (file_task, instruction_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(
                    task
                    for task in (file_task, instruction_task)
                    if task is not None
                ),
                return_exceptions=True,
            )

        self.assertEqual(file_result.state, "collecting")
        self.assertEqual(instruction_result.state, "collecting")
        self.assertEqual(
            file_result.input_batch_id,
            instruction_result.input_batch_id,
        )

        batch, duplicate = await self.ingress.batch_store.commit_batch(
            file_result.input_batch_id,
            session_id=self.session_id,
            reason="test_reservation_race",
        )
        self.assertFalse(duplicate)
        self.assertEqual(len(batch.artifact_refs), 1)
        self.assertEqual(
            [part.text for part in batch.text_parts],
            ["Process the attached files"],
        )
        self.assertEqual(len(batch.source_event_ids), 2)


if __name__ == "__main__":
    unittest.main()
