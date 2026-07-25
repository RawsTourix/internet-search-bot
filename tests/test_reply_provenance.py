import tempfile
import unittest
from datetime import datetime, timezone

from src.adapters.telegram_ingress import build_telegram_input_envelope
from src.ingress.store import FileSystemIngressEventStore
from src.interaction.anchors import (
    ClientResponseAnchorCandidate,
    ClientResponseAnchorKind,
    ResponseAnchorSelector,
)
from src.interaction.parts import LocationInputPart
from src.storage.config import StorageConfigType


class ReplyProvenanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_reply_context_does_not_override_current_instruction_anchor(self):
        with tempfile.TemporaryDirectory() as temporary:
            envelope = build_telegram_input_envelope(
                bot_instance_id="bot",
                update_id="1",
                chat_id="chat",
                user_id="user",
                message_id="current",
                attachments=[],
                text="new instruction",
                reply_to_message_id="old",
                reply_to_sender_id="other",
                reply_to_excerpt="old content",
                occurred_at=datetime.now(timezone.utc),
            )
            event, _ = await FileSystemIngressEventStore(
                StorageConfigType(root_dir=temporary)
            ).save_if_absent(envelope)
        self.assertEqual(
            event.response_anchor_candidates[0].client_message_id,
            "current",
        )
        self.assertEqual(
            event.response_anchor_candidates[0].kind,
            ClientResponseAnchorKind.INSTRUCTION,
        )
        self.assertEqual(event.reply_context.replied_to_message_id, "old")

    async def test_reply_attachment_anchors_current_event(self):
        with tempfile.TemporaryDirectory() as temporary:
            envelope = build_telegram_input_envelope(
                bot_instance_id="bot",
                update_id="2",
                chat_id="chat",
                user_id="user",
                message_id="attachment",
                attachments=[],
                semantic_parts=[
                    LocationInputPart(
                        part_id="location",
                        latitude=1,
                        longitude=2,
                    )
                ],
                reply_to_message_id="old",
                occurred_at=datetime.now(timezone.utc),
            )
            event, _ = await FileSystemIngressEventStore(
                StorageConfigType(root_dir=temporary)
            ).save_if_absent(envelope)
        self.assertEqual(
            event.response_anchor_candidates[0].client_message_id,
            "attachment",
        )
        self.assertEqual(
            event.response_anchor_candidates[0].kind,
            ClientResponseAnchorKind.ATTACHMENT,
        )

    async def test_typed_explicit_override_is_the_only_old_target_authority(self):
        selector = ResponseAnchorSelector()
        override = ClientResponseAnchorCandidate(
            client_message_id="trusted-target",
            source_message_id="current",
            kind=ClientResponseAnchorKind.EXPLICIT,
            priority=selector.priority_for(ClientResponseAnchorKind.EXPLICIT),
            occurred_at=datetime.now(timezone.utc),
        )
        with tempfile.TemporaryDirectory() as temporary:
            envelope = build_telegram_input_envelope(
                bot_instance_id="bot",
                update_id="3",
                chat_id="chat",
                user_id="user",
                message_id="current",
                attachments=[],
                text="new instruction",
                occurred_at=datetime.now(timezone.utc),
            ).model_copy(update={"response_anchor_override": override})
            event, _ = await FileSystemIngressEventStore(
                StorageConfigType(root_dir=temporary)
            ).save_if_absent(envelope)
        self.assertEqual(
            event.response_anchor_candidates[0].client_message_id,
            "trusted-target",
        )
