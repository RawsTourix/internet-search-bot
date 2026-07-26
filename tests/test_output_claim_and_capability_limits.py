import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from telegram.error import BadRequest

from src.artifacts import (
    ArtifactAccessContext,
    ArtifactConfigType,
    ArtifactProvenance,
    ArtifactPurpose,
    create_artifact_services,
)
from src.artifacts.models import new_artifact_delivery_id, new_artifact_id
from src.core.models import AgentResult, AgentStatus, ClientType
from src.ingress.models import (
    ClientResponseRoute,
    CommittedInputBatch,
    new_ingress_event_id,
    new_input_batch_id,
)
from src.interaction.capabilities import (
    build_default_capability_registry,
    build_telegram_capability_declaration,
)
from src.interaction.config import OutputRuntimeConfig
from src.interaction.errors import InteractionValidationError
from src.interaction.ids import new_output_attempt_id, new_output_part_id
from src.interaction.output_models import (
    ArtifactOutputPart,
    ImageOutputPart,
    OutputBatchKind,
    OutputBatchState,
    OutputDeliveryReceiptState,
    OutputPartReceiptState,
    TextOutputPart,
)
from src.interaction.output_service import OutputBatchAssembler
from src.interaction.output_store import (
    FileSystemOutputBatchStore,
    build_ready_output_batch,
)
from src.interaction.rendering import CapabilityOutputRenderer
from src.servers.telegram.output_plan_executor import (
    TelegramExecutionContext,
    TelegramOutputPlanExecutor,
)
from src.storage import create_storage_services
from src.storage.config import StorageConfigType
from tests.telegram_fakes import FakeTelegramBot, FakeTelegramGateway


UTC = timezone.utc


class OutputClaimAndCapabilityLimitTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.registry = build_default_capability_registry()
        self.route = ClientResponseRoute(
            route_type="telegram",
            conversation_id="chat-1",
        )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_text_limit_from_snapshot_controls_chunking(self):
        snapshot = self._snapshot(text_limit=4)
        batch = self._batch(
            snapshot=snapshot,
            parts=(
                TextOutputPart(
                    part_id=new_output_part_id(),
                    index=0,
                    text="abcdefghij",
                ),
            ),
        )
        bot, receipt = await self._execute(batch)
        calls = [call for call in bot.calls if call[0] == "send_message"]
        self.assertEqual([item[1]["text"] for item in calls], ["abcd", "efgh", "ij"])
        self.assertEqual(receipt.state, OutputDeliveryReceiptState.DELIVERED)
        self.assertEqual(len(receipt.part_receipts[0].client_message_ids), 3)

    async def test_long_caption_is_delivered_as_media_plus_text(self):
        snapshot = self._snapshot(text_limit=4, caption_limit=5)
        part = ImageOutputPart(
            part_id=new_output_part_id(),
            index=0,
            artifact_id=new_artifact_id(),
            delivery_id=new_artifact_delivery_id(),
            filename="image.png",
            mime_type="image/png",
            size_bytes=1,
            caption="abcdefghij",
        )
        batch = self._batch(snapshot=snapshot, parts=(part,))
        bot, receipt = await self._execute(batch)
        self.assertEqual(
            [name for name, _ in bot.calls],
            ["send_photo", "send_message", "send_message", "send_message"],
        )
        self.assertNotIn("caption", bot.calls[0][1])
        self.assertEqual(
            [call[1]["text"] for call in bot.calls[1:]],
            ["abcd", "efgh", "ij"],
        )
        self.assertEqual(receipt.state, OutputDeliveryReceiptState.DELIVERED)
        self.assertEqual(len(receipt.part_receipts[0].client_message_ids), 4)

    async def test_caption_failure_after_media_is_partial_not_failed(self):
        snapshot = self._snapshot(text_limit=4, caption_limit=5)
        part = ImageOutputPart(
            part_id=new_output_part_id(),
            index=0,
            artifact_id=new_artifact_id(),
            delivery_id=new_artifact_delivery_id(),
            filename="image.png",
            mime_type="image/png",
            size_bytes=1,
            caption="abcdefghij",
        )
        bot = FakeTelegramBot()
        bot.queue("send_message", BadRequest("caption rejected"))
        _, receipt = await self._execute(
            self._batch(snapshot=snapshot, parts=(part,)),
            bot=bot,
        )
        self.assertEqual(
            receipt.part_receipts[0].state,
            OutputPartReceiptState.PARTIALLY_DELIVERED,
        )
        self.assertEqual(receipt.state, OutputDeliveryReceiptState.PARTIALLY_DELIVERED)
        self.assertEqual(len(receipt.part_receipts[0].client_message_ids), 1)

    async def test_claim_validation_failure_preserves_ready_state(self):
        store = FileSystemOutputBatchStore(self.root)
        batch, _ = await store.commit(
            self._batch(
                snapshot=self._snapshot(),
                parts=(
                    TextOutputPart(
                        part_id=new_output_part_id(),
                        index=0,
                        text="result",
                    ),
                ),
            )
        )

        def reject(_batch):
            raise InteractionValidationError("delivery plan exceeds current policy")

        store.bind_claim_validator(reject)
        with self.assertRaises(InteractionValidationError):
            await store.claim_delivery(batch.output_batch_id)
        self.assertEqual(
            (await store.get(batch.output_batch_id)).state,
            OutputBatchState.READY,
        )

    async def test_semantic_intent_cannot_downgrade_selected_deliverable(self):
        storage_config = StorageConfigType(root_dir=str(self.root / "assembly"))
        storage = create_storage_services(storage_config)
        artifacts = create_artifact_services(
            storage_config=storage_config,
            artifact_config=ArtifactConfigType(),
            content_store=storage.content_store,
        )
        artifact = await artifacts.artifact_service.create_text(
            session_id="session-1",
            cycle_id="cycle-1",
            filename="result.md",
            text="result",
            format_id="markdown",
            purpose=ArtifactPurpose.DELIVERABLE,
            provenance=ArtifactProvenance(
                origin="agent_created",
                creator="agent",
                operation="required_intent_test",
            ),
        )
        selected = await artifacts.delivery_service.select(
            artifact_id=artifact.artifact_id,
            access=ArtifactAccessContext(
                session_id="session-1",
                cycle_id="cycle-1",
                allowed_artifact_ids=[artifact.artifact_id],
            ),
            client_type="telegram",
        )
        snapshot = self._snapshot()
        now = datetime.now(UTC)
        input_batch = CommittedInputBatch(
            input_batch_id=new_input_batch_id(),
            session_id="session-1",
            client_type=ClientType.TELEGRAM,
            sequence_number=1,
            source_event_ids=[new_ingress_event_id()],
            admission_mode="auto",
            response_route=self.route,
            locale="ru",
            capability_snapshot=snapshot,
            committed_at=now,
            commit_reason="test",
            content_fingerprint="sha256:" + ("d" * 64),
        )
        assembler = OutputBatchAssembler(
            config=OutputRuntimeConfig(),
            delivery_store=artifacts.delivery_store,
            output_store=FileSystemOutputBatchStore(Path(storage_config.root_dir)),
        )
        batch = await assembler.assemble_final(
            result=AgentResult(
                content="",
                status=AgentStatus.DONE,
                session_id="session-1",
                cycle_id="cycle-1",
                semantic_outputs=[{
                    "type": "artifact_output",
                    "part_id": new_output_part_id(),
                    "index": 0,
                    "required": False,
                    "artifact_id": artifact.artifact_id,
                    "delivery_id": selected.delivery_id,
                    "filename": "untrusted.md",
                    "mime_type": "text/markdown",
                    "size_bytes": 1,
                }],
            ),
            input_batch=input_batch,
        )
        self.assertEqual(len(batch.parts), 1)
        self.assertIsInstance(batch.parts[0], ArtifactOutputPart)
        self.assertTrue(batch.parts[0].required)

    def _snapshot(self, *, text_limit=4096, caption_limit=1024):
        declaration = build_telegram_capability_declaration()
        declaration = declaration.model_copy(update={
            "limits": {
                **declaration.limits,
                "transport.telegram.output.text.max_chars": text_limit,
                "transport.telegram.output.caption.max_chars": caption_limit,
            }
        })
        return self.registry.resolve(
            declaration,
            client_type="telegram",
            client_instance_id="bot-1",
        )

    def _batch(self, *, snapshot, parts):
        return build_ready_output_batch(
            session_id="session-1",
            cycle_id="cycle-1",
            sequence_number=1,
            kind=OutputBatchKind.FINAL,
            response_route=self.route,
            locale="ru",
            capability_snapshot=snapshot,
            parts=parts,
        )

    async def _execute(self, batch, *, bot=None):
        fake_bot = bot or FakeTelegramBot()
        plan = CapabilityOutputRenderer().plan(batch)
        receipt = await TelegramOutputPlanExecutor().execute(
            batch=batch,
            plan=plan,
            attempt_id=new_output_attempt_id(),
            context=TelegramExecutionContext(
                bot=fake_bot,
                gateway=FakeTelegramGateway(),
                session_id="session-1",
                chat_id=1,
            ),
        )
        return fake_bot, receipt


if __name__ == "__main__":
    unittest.main()
