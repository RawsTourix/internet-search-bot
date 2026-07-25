import unittest
from types import SimpleNamespace

from src.adapters.telegram_ingress import build_telegram_input_envelope
from src.adapters.telegram_resolvers import TelegramInputResolverRegistry
from src.interaction.parts import LocationInputPart


class TelegramSemanticResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_structured_part_builds_envelope_without_binary_attachment(self):
        envelope = build_telegram_input_envelope(
            bot_instance_id="bot-1",
            update_id="update-1",
            chat_id="chat-1",
            user_id="user-1",
            message_id="message-1",
            attachments=[],
            semantic_parts=[LocationInputPart(
                part_id="semantic-location-message-1",
                latitude=57.6261,
                longitude=39.8845,
            )],
        )
        self.assertEqual(envelope.attachment_slots, [])
        self.assertEqual(envelope.semantic_parts[0].type, "location_input")

    async def test_resolves_text_photo_location_contact_poll_and_forward(self):
        message = SimpleNamespace(
            message_id=42,
            text="Inspect this place",
            caption=None,
            document=None,
            photo=[
                SimpleNamespace(
                    file_unique_id="small",
                    file_size=10,
                    width=20,
                    height=20,
                ),
                SimpleNamespace(
                    file_unique_id="large",
                    file_size=100,
                    width=200,
                    height=200,
                ),
            ],
            audio=None,
            voice=None,
            video=None,
            video_note=None,
            animation=None,
            sticker=None,
            location=SimpleNamespace(
                latitude=57.6261,
                longitude=39.8845,
                horizontal_accuracy=15.0,
                live_period=None,
                heading=None,
            ),
            contact=SimpleNamespace(
                phone_number="+70000000000",
                first_name="Test",
                last_name="User",
                user_id=7,
                vcard=None,
            ),
            poll=SimpleNamespace(
                id="poll-1",
                question="Continue?",
                options=[
                    SimpleNamespace(text="Yes"),
                    SimpleNamespace(text="No"),
                ],
                is_anonymous=False,
                allows_multiple_answers=False,
            ),
            forward_origin=SimpleNamespace(
                id=99,
                full_name="Untrusted Origin",
                title=None,
            ),
            forward_from=None,
            forward_from_chat=None,
            forward_sender_name=None,
            forward_from_message_id=5,
            forward_date=None,
        )
        parts = await TelegramInputResolverRegistry().resolve(message)
        by_type = {part.type: part for part in parts}
        self.assertEqual(
            set(by_type),
            {
                "text_input",
                "image_input",
                "location_input",
                "contact_input",
                "poll_input",
                "forwarded_message_input",
            },
        )
        self.assertEqual(by_type["image_input"].width, 200)
        self.assertEqual(by_type["poll_input"].options, ("Yes", "No"))
        self.assertFalse(by_type["forwarded_message_input"].trusted)

    async def test_voice_is_exact_media_metadata_not_transcription(self):
        message = SimpleNamespace(
            message_id=7,
            text=None,
            caption=None,
            document=None,
            photo=None,
            audio=None,
            voice=SimpleNamespace(
                file_unique_id="voice-unique",
                file_size=123,
                duration=4,
                mime_type="audio/ogg",
            ),
            video=None,
            video_note=None,
            animation=None,
            sticker=None,
            location=None,
            contact=None,
            poll=None,
            forward_origin=None,
            forward_from=None,
            forward_from_chat=None,
            forward_sender_name=None,
        )
        parts = await TelegramInputResolverRegistry().resolve(message)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].type, "voice_input")
        self.assertEqual(parts[0].duration_seconds, 4)
        payload = parts[0].model_dump(mode="json")
        self.assertNotIn("transcript", payload)
        self.assertNotIn("ocr", payload)


if __name__ == "__main__":
    unittest.main()
