import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.adapters.telegram_ingress import build_telegram_input_envelope
from src.core.models import ClientType
from src.ingress.config import IngressConfigType
from src.ingress.coordinated_store import (
    FileSystemCoordinatedInputBatchStore,
)
from src.ingress.models import (
    ClientConversationRef,
    ClientInputEnvelope,
    ClientResponseRoute,
    ClientSenderRef,
    InputGroupingMode,
)
from src.ingress.semantic_limits import (
    SemanticInputLimitError,
    validate_semantic_parts,
)
from src.ingress.store import (
    FileSystemIngressEventStore,
    IngressConflictError,
)
from src.interaction.parts import (
    ContactInputPart,
    ImageInputPart,
    LocationInputPart,
    PollInputPart,
    TextInputPart,
)
from src.storage.config import StorageConfigType


class SemanticIngressLimitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)

    def test_caption_has_one_canonical_agent_representation(self):
        envelope = build_telegram_input_envelope(
            bot_instance_id="bot",
            update_id="1",
            chat_id="1",
            user_id="1",
            message_id="10",
            attachments=[{
                "file_id": "file",
                "media_kind": "photo",
                "filename": "photo.jpg",
            }],
            caption="describe this",
            semantic_parts=[
                TextInputPart(
                    part_id="semantic-caption",
                    text="describe this",
                    role="caption",
                ),
                ImageInputPart(
                    part_id="image",
                    slot_id="slot_10-1",
                ),
            ],
            occurred_at=self.now,
        )
        self.assertEqual(len(envelope.text_parts), 1)
        self.assertEqual(envelope.text_parts[0].text, "describe this")
        self.assertEqual(
            [item.type for item in envelope.semantic_parts],
            ["image_input"],
        )

    def test_vcard_poll_and_metadata_limits(self):
        config = IngressConfigType(
            max_vcard_chars=4,
            max_poll_options=2,
            max_poll_option_chars=3,
            max_semantic_metadata_bytes_per_part=8,
        )
        with self.assertRaises(SemanticInputLimitError):
            validate_semantic_parts(
                [
                    ContactInputPart(
                        part_id="contact",
                        phone_number="+1",
                        first_name="A",
                        vcard="12345",
                    )
                ],
                config,
            )
        with self.assertRaises(SemanticInputLimitError):
            validate_semantic_parts(
                [
                    PollInputPart(
                        part_id="poll",
                        question="Q",
                        options=("one", "two", "three"),
                    )
                ],
                config,
            )
        with self.assertRaises(SemanticInputLimitError):
            validate_semantic_parts(
                [
                    LocationInputPart(
                        part_id="location",
                        latitude=1,
                        longitude=2,
                        metadata={"long": "metadata"},
                    )
                ],
                config,
            )

    async def test_grouped_append_enforces_cumulative_limit_and_collision(self):
        with tempfile.TemporaryDirectory() as temporary:
            storage = StorageConfigType(root_dir=temporary)
            config = IngressConfigType(max_semantic_parts_per_batch=1)
            events = FileSystemIngressEventStore(storage)
            batches = FileSystemCoordinatedInputBatchStore(storage, config)

            first, _ = await events.save_if_absent(
                self._envelope(
                    "one",
                    LocationInputPart(
                        part_id="shared",
                        latitude=1,
                        longitude=2,
                    ),
                )
            )
            draft, _ = await batches.create_for_event(
                first,
                session_id="session",
                grouping_mode=InputGroupingMode.MEDIA_GROUP,
                grouping_key="group",
            )
            second, _ = await events.save_if_absent(
                self._envelope(
                    "two",
                    ContactInputPart(
                        part_id="contact",
                        phone_number="+1",
                        first_name="A",
                    ),
                )
            )
            with self.assertRaises(IngressConflictError):
                await batches.append_event_to_batch(
                    draft.input_batch_id,
                    second,
                )

            collision, _ = await events.save_if_absent(
                self._envelope(
                    "three",
                    LocationInputPart(
                        part_id="shared",
                        latitude=3,
                        longitude=4,
                    ),
                )
            )
            with self.assertRaises(IngressConflictError):
                await batches.append_event_to_batch(
                    draft.input_batch_id,
                    collision,
                )

    def _envelope(self, message_id, part):
        return ClientInputEnvelope(
            idempotency_key=f"event-{message_id}",
            client_type=ClientType.TELEGRAM,
            client_instance_id="bot",
            conversation=ClientConversationRef(conversation_id="chat"),
            sender=ClientSenderRef(principal_id="user"),
            source_message_id=message_id,
            occurred_at=self.now,
            semantic_parts=[part],
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="chat",
            ),
        )
