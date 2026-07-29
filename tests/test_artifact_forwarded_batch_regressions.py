import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import httpx

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
from src.interaction.parts import ForwardedMessageInputPart
from src.servers.telegram.scoped_artifact_bridge import (
    InstanceScopedTelegramArtifactGatewayClient,
)
from src.storage import StorageConfigType, create_storage_services


async def chunks(value: bytes):
    yield value


class IngressOriginalIdempotencyTests(unittest.IsolatedAsyncioTestCase):
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
        self.session_id = "telegram:conversation:chat-forwarded"

    async def asyncTearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def _text_envelope() -> ClientInputEnvelope:
        return ClientInputEnvelope(
            idempotency_key="telegram:bot-1:update:text-1",
            client_type=ClientType.TELEGRAM,
            client_instance_id="bot-1",
            conversation=ClientConversationRef(
                conversation_id="chat-forwarded"
            ),
            sender=ClientSenderRef(principal_id="user-1"),
            source_update_id="text-update-1",
            source_message_id="text-message-1",
            occurred_at=datetime.now(timezone.utc),
            text_parts=[
                IngressTextPart(
                    part_id="text-part-1",
                    kind="message_text",
                    text="Process this request",
                    attachment_slot_ids=[],
                )
            ],
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="chat-forwarded",
                reply_to_message_id="text-message-1",
            ),
        )

    @staticmethod
    def _file_envelope() -> ClientInputEnvelope:
        return ClientInputEnvelope(
            idempotency_key="telegram:bot-1:update:file-1",
            client_type=ClientType.TELEGRAM,
            client_instance_id="bot-1",
            conversation=ClientConversationRef(
                conversation_id="chat-forwarded"
            ),
            sender=ClientSenderRef(principal_id="user-1"),
            source_update_id="file-update-1",
            source_message_id="file-message-1",
            source_group_id="album-1",
            occurred_at=datetime.now(timezone.utc),
            attachment_slots=[
                IngressAttachmentSlot(
                    slot_id="slot-file-1",
                    media_kind="document",
                    original_filename="source.txt",
                    declared_mime_type="text/plain",
                    declared_size_bytes=5,
                    transport_locator=ClientAttachmentLocator(
                        provider="telegram",
                        locator="telegram-file-1",
                    ),
                )
            ],
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="chat-forwarded",
                reply_to_message_id="file-message-1",
            ),
        )

    async def test_new_atomic_text_is_not_its_own_duplicate(self):
        envelope = self._text_envelope()

        first = await self.service.submit_atomic(
            envelope,
            session_id=self.session_id,
        )
        second = await self.service.submit_atomic(
            envelope,
            session_id=self.session_id,
        )

        self.assertEqual(first.state, "committed")
        self.assertFalse(first.duplicate)
        self.assertEqual(second.state, "committed")
        self.assertTrue(second.duplicate)
        self.assertEqual(first.input_batch_id, second.input_batch_id)

    async def test_first_group_member_is_not_internal_duplicate(self):
        result = await self.service.submit_atomic(
            self._file_envelope(),
            session_id=self.session_id,
            upload_streams={"slot-file-1": chunks(b"hello")},
        )

        self.assertEqual(result.state, "collecting")
        self.assertFalse(result.duplicate)


class ForwardedTextAlbumRaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.requests: list[dict] = []
        self.group_batches: dict[str, str] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path != "/ingress/events":
                raise AssertionError(f"unexpected request: {request.url}")
            payload = json.loads(request.content.decode("utf-8"))
            self.requests.append(payload)
            group_id = str(payload.get("source_group_id") or "atomic")
            batch_id = self.group_batches.setdefault(
                group_id,
                "ibat_" + f"{len(self.group_batches) + 1:032x}",
            )
            return httpx.Response(
                200,
                json={
                    "status": "collecting",
                    "input_batch_id": batch_id,
                    "duplicate": False,
                },
            )

        self.client = InstanceScopedTelegramArtifactGatewayClient(
            gateway_url="http://gateway.test",
            api_key="key",
            client_instance_id="default",
            transport=httpx.MockTransport(handler),
            input_text_join_window_seconds=10,
            forwarded_text_join_wait_seconds=0.5,
        )

    @staticmethod
    def _file_envelope() -> ClientInputEnvelope:
        return ClientInputEnvelope(
            idempotency_key="telegram:file:10",
            client_type=ClientType.TELEGRAM,
            client_instance_id="default",
            conversation=ClientConversationRef(conversation_id="100"),
            sender=ClientSenderRef(principal_id="200"),
            source_update_id="u-10",
            source_message_id="10",
            source_group_id="album-1",
            occurred_at=datetime.now(timezone.utc),
            attachment_slots=[
                IngressAttachmentSlot(
                    slot_id="slot-10",
                    media_kind="document",
                    original_filename="10.txt",
                    declared_mime_type="text/plain",
                    declared_size_bytes=1,
                    transport_locator=ClientAttachmentLocator(
                        provider="telegram",
                        locator="file-10",
                    ),
                )
            ],
            semantic_parts=[
                ForwardedMessageInputPart(
                    part_id="forward-10",
                    origin_type="MessageOriginUser",
                    source_message_id="10",
                )
            ],
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="100",
                reply_to_message_id="10",
            ),
        )

    @staticmethod
    def _text_envelope(*, forwarded: bool, message_id: str) -> ClientInputEnvelope:
        semantic_parts = (
            [
                ForwardedMessageInputPart(
                    part_id=f"forward-{message_id}",
                    origin_type="MessageOriginUser",
                    source_message_id=message_id,
                )
            ]
            if forwarded
            else []
        )
        return ClientInputEnvelope(
            idempotency_key=f"telegram:text:{message_id}",
            client_type=ClientType.TELEGRAM,
            client_instance_id="default",
            conversation=ClientConversationRef(conversation_id="100"),
            sender=ClientSenderRef(principal_id="200"),
            source_update_id=f"u-{message_id}",
            source_message_id=message_id,
            occurred_at=datetime.now(timezone.utc),
            text_parts=[
                IngressTextPart(
                    part_id=f"text-{message_id}",
                    kind="message_text",
                    text="Process the forwarded files",
                    attachment_slot_ids=[],
                )
            ],
            semantic_parts=semantic_parts,
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="100",
                reply_to_message_id=message_id,
            ),
        )

    async def test_forwarded_text_waits_for_earlier_album_registration(self):
        text_task = asyncio.create_task(
            self.client.submit_envelope(
                self._text_envelope(forwarded=True, message_id="13"),
                progress_locale="ru",
            )
        )
        await asyncio.sleep(0.05)
        self.assertEqual(self.requests, [])

        file_result = await self.client.submit_envelope(
            self._file_envelope(),
            progress_locale="ru",
        )
        text_result = await asyncio.wait_for(text_task, timeout=1)

        self.assertEqual(
            file_result["input_batch_id"],
            text_result["input_batch_id"],
        )
        text_request = next(
            item for item in self.requests if item.get("text_parts")
        )
        self.assertEqual(text_request["source_group_id"], "album-1")

    async def test_ordinary_text_does_not_wait_for_forwarded_join_window(self):
        result = await asyncio.wait_for(
            self.client.submit_envelope(
                self._text_envelope(forwarded=False, message_id="20"),
                progress_locale="ru",
            ),
            timeout=0.1,
        )

        self.assertEqual(result["input_batch_id"], self.group_batches["atomic"])
        self.assertIsNone(self.requests[-1].get("source_group_id"))


if __name__ == "__main__":
    unittest.main()
