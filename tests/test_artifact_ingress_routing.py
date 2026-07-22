import unittest
from datetime import datetime, timezone

from src.core.models import ClientType
from src.ingress import (
    ClientConversationRef,
    ClientInputEnvelope,
    ClientResponseRoute,
    ClientSenderRef,
    IngressAttachmentSlot,
    IngressTextPart,
    InputGroupingMode,
    resolve_input_grouping,
)


class ArtifactIngressRoutingTests(unittest.TestCase):
    def _base(self, **updates):
        payload = dict(
            idempotency_key="telegram:bot-1:update:1:message:10",
            client_type=ClientType.TELEGRAM,
            client_instance_id="bot-1",
            conversation=ClientConversationRef(
                conversation_id="chat-1",
                thread_id="thread-2",
            ),
            sender=ClientSenderRef(principal_id="user-1"),
            source_update_id="1",
            source_message_id="10",
            occurred_at=datetime.now(timezone.utc),
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="chat-1",
                thread_id="thread-2",
                reply_to_message_id="10",
            ),
        )
        payload.update(updates)
        return ClientInputEnvelope(**payload)

    def test_telegram_media_group_uses_stable_group_key(self):
        envelope = self._base(
            source_group_id="album-99",
            attachment_slots=[IngressAttachmentSlot(
                slot_id="slot_10-1",
                media_kind="photo",
                original_filename="photo.jpg",
                declared_mime_type="image/jpeg",
                transport_locator={"provider": "telegram", "locator": "file-1"},
            )],
        )

        decision = resolve_input_grouping(envelope)

        self.assertEqual(decision.mode, InputGroupingMode.MEDIA_GROUP)
        self.assertIn("album-99", decision.key)
        self.assertIn("chat-1", decision.key)
        self.assertIn("user-1", decision.key)

    def test_telegram_single_attachment_is_explicit_grouped_draft(self):
        envelope = self._base(
            attachment_slots=[IngressAttachmentSlot(
                slot_id="slot_10-1",
                media_kind="document",
                original_filename="report.pdf",
                declared_mime_type="application/pdf",
                transport_locator={"provider": "telegram", "locator": "file-1"},
            )],
        )

        decision = resolve_input_grouping(envelope)

        self.assertEqual(
            decision.mode,
            InputGroupingMode.STANDALONE_ATTACHMENT,
        )
        self.assertIn("message:10", decision.key)

    def test_text_and_web_inputs_remain_atomic(self):
        telegram_text = self._base(
            text_parts=[IngressTextPart(
                part_id="message-10",
                kind="message_text",
                text="hello",
            )],
        )
        web = ClientInputEnvelope(
            idempotency_key="web:1",
            client_type=ClientType.WEB,
            client_instance_id="web-1",
            conversation=ClientConversationRef(conversation_id="session-1"),
            sender=ClientSenderRef(principal_id="user-1"),
            source_message_id="request-1",
            occurred_at=datetime.now(timezone.utc),
            attachment_slots=[IngressAttachmentSlot(
                slot_id="slot_file-1",
                media_kind="document",
                original_filename="file.txt",
                upload_field_name="file_1",
            )],
            response_route=ClientResponseRoute(
                route_type="web",
                conversation_id="session-1",
            ),
        )

        self.assertEqual(
            resolve_input_grouping(telegram_text).mode,
            InputGroupingMode.ATOMIC,
        )
        self.assertEqual(
            resolve_input_grouping(web).mode,
            InputGroupingMode.ATOMIC,
        )


if __name__ == "__main__":
    unittest.main()
