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
from src.artifacts.models import ArtifactDeliveryState
from src.ingress.models import ClientResponseRoute
from src.interaction.capabilities import (
    build_default_capability_registry,
    build_telegram_capability_declaration,
)
from src.interaction.ids import new_output_attempt_id, new_output_part_id
from src.interaction.output_completion import OutputDeliveryCompletionService
from src.interaction.output_models import (
    ArtifactOutputPart,
    LocationOutputPart,
    OutputBatchKind,
    OutputBatchState,
    OutputDeliveryReceipt,
    OutputDeliveryReceiptState,
    OutputPartReceipt,
    OutputPartReceiptState,
    TextOutputPart,
)
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


class OutputReceiptSemanticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = build_default_capability_registry()
        self.snapshot = self.registry.resolve(
            build_telegram_capability_declaration(),
            client_type="telegram",
            client_instance_id="bot-1",
        )
        self.renderer = CapabilityOutputRenderer()
        self.executor = TelegramOutputPlanExecutor()

    def _batch(self, parts, *, cycle_id="cycle-1", snapshot=None):
        return build_ready_output_batch(
            session_id="session-1",
            cycle_id=cycle_id,
            sequence_number=1,
            kind=OutputBatchKind.FINAL,
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="chat-1",
            ),
            locale="en",
            capability_snapshot=snapshot or self.snapshot,
            parts=tuple(parts),
        )

    async def _execute(self, parts, *, bot=None, snapshot=None):
        batch = self._batch(parts, snapshot=snapshot)
        return await self.executor.execute(
            batch=batch,
            plan=self.renderer.plan(batch),
            attempt_id=new_output_attempt_id(),
            context=TelegramExecutionContext(
                bot=bot or FakeTelegramBot(),
                gateway=FakeTelegramGateway(),
                session_id="session-1",
                chat_id=1,
            ),
        )

    async def test_optional_failure_does_not_invalidate_required_delivery(self):
        bot = FakeTelegramBot()
        bot.queue("send_location", BadRequest("location rejected"))
        receipt = await self._execute(
            [
                TextOutputPart(
                    part_id=new_output_part_id(),
                    index=0,
                    text="required",
                    required=True,
                ),
                LocationOutputPart(
                    part_id=new_output_part_id(),
                    index=1,
                    latitude=1,
                    longitude=2,
                    required=False,
                ),
            ],
            bot=bot,
        )
        self.assertEqual(receipt.state, OutputDeliveryReceiptState.DELIVERED)
        self.assertEqual(
            receipt.part_receipts[0].state,
            OutputPartReceiptState.DELIVERED,
        )
        self.assertTrue(receipt.part_receipts[0].required)
        self.assertEqual(
            receipt.part_receipts[1].state,
            OutputPartReceiptState.FAILED,
        )
        self.assertFalse(receipt.part_receipts[1].required)

    async def test_confirmed_second_chunk_failure_is_partial(self):
        base = build_telegram_capability_declaration()
        limited_snapshot = self.registry.resolve(
            base.model_copy(update={
                "limits": {
                    **base.limits,
                    "transport.telegram.output.text.max_chars": 5,
                }
            }),
            client_type="telegram",
            client_instance_id="bot-1",
        )
        bot = FakeTelegramBot()
        bot.queue("send_message", None, BadRequest("second chunk rejected"))
        receipt = await self._execute(
            [
                TextOutputPart(
                    part_id=new_output_part_id(),
                    index=0,
                    text="abcdefghij",
                )
            ],
            bot=bot,
            snapshot=limited_snapshot,
        )
        part = receipt.part_receipts[0]
        self.assertEqual(
            part.state,
            OutputPartReceiptState.PARTIALLY_DELIVERED,
        )
        self.assertEqual(
            receipt.state,
            OutputDeliveryReceiptState.PARTIALLY_DELIVERED,
        )
        self.assertEqual(len(part.client_message_ids), 1)
        self.assertIsNotNone(part.delivered_at)

    async def test_markdown_text_uses_html_rendering(self):
        bot = FakeTelegramBot()
        receipt = await self._execute(
            [
                TextOutputPart(
                    part_id=new_output_part_id(),
                    index=0,
                    text="**hello**",
                    parse_mode="markdown",
                )
            ],
            bot=bot,
        )
        self.assertEqual(receipt.state, OutputDeliveryReceiptState.DELIVERED)
        _, kwargs = bot.calls[0]
        self.assertEqual(kwargs["parse_mode"], "HTML")
        self.assertEqual(kwargs["text"], "<b>hello</b>")

    async def test_public_reconcile_updates_output_and_artifact_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = StorageConfigType(root_dir=temporary)
            storage = create_storage_services(config)
            artifacts = create_artifact_services(
                storage_config=config,
                artifact_config=ArtifactConfigType(),
                content_store=storage.content_store,
            )
            artifact = await artifacts.artifact_service.create_text(
                session_id="session-1",
                cycle_id="cycle-reconcile",
                filename="result.md",
                text="result",
                format_id="markdown",
                purpose=ArtifactPurpose.DELIVERABLE,
                provenance=ArtifactProvenance(
                    origin="agent_created",
                    creator="agent",
                    operation="test",
                ),
            )
            selected = await artifacts.delivery_service.select(
                artifact_id=artifact.artifact_id,
                access=ArtifactAccessContext(
                    session_id="session-1",
                    cycle_id="cycle-reconcile",
                    allowed_artifact_ids=[artifact.artifact_id],
                ),
                client_type="telegram",
            )
            await artifacts.delivery_service.claim(selected.delivery_id)

            output_store = FileSystemOutputBatchStore(Path(temporary))
            completion = OutputDeliveryCompletionService(
                output_store=output_store,
                artifact_delivery_store=artifacts.delivery_store,
            )
            batch = self._batch(
                [
                    ArtifactOutputPart(
                        part_id=new_output_part_id(),
                        index=0,
                        artifact_id=artifact.artifact_id,
                        delivery_id=selected.delivery_id,
                        filename="result.md",
                        mime_type="text/markdown",
                        size_bytes=6,
                    )
                ],
                cycle_id="cycle-reconcile",
            )
            batch, _ = await output_store.commit(batch)
            _, attempt_id = await output_store.claim_delivery(
                batch.output_batch_id
            )
            now = datetime.now(timezone.utc)

            unknown_receipt = OutputDeliveryReceipt(
                output_batch_id=batch.output_batch_id,
                attempt_id=attempt_id,
                state=OutputDeliveryReceiptState.UNKNOWN,
                part_receipts=(
                    OutputPartReceipt(
                        part_id=batch.parts[0].part_id,
                        index=0,
                        state=OutputPartReceiptState.UNKNOWN,
                        required=True,
                        delivery_id=selected.delivery_id,
                        error_category="transport_timeout_after_send",
                    ),
                ),
                started_at=now,
                completed_at=now,
            )
            unknown = await completion.complete(unknown_receipt)
            self.assertEqual(unknown.state, OutputBatchState.UNKNOWN)
            self.assertEqual(
                (await artifacts.delivery_store.get(selected.delivery_id)).state,
                ArtifactDeliveryState.UNKNOWN,
            )

            delivered_receipt = OutputDeliveryReceipt(
                output_batch_id=batch.output_batch_id,
                attempt_id=attempt_id,
                state=OutputDeliveryReceiptState.DELIVERED,
                part_receipts=(
                    OutputPartReceipt(
                        part_id=batch.parts[0].part_id,
                        index=0,
                        state=OutputPartReceiptState.DELIVERED,
                        required=True,
                        delivery_id=selected.delivery_id,
                        client_message_ids=("501",),
                        delivered_at=now,
                    ),
                ),
                started_at=now,
                completed_at=now,
            )
            reconciled = await output_store.reconcile_unknown(
                delivered_receipt
            )
            self.assertEqual(reconciled.state, OutputBatchState.DELIVERED)
            self.assertEqual(
                (await artifacts.delivery_store.get(selected.delivery_id)).state,
                ArtifactDeliveryState.DELIVERED,
            )
            self.assertTrue(
                (
                    output_store.attempts
                    / f"{attempt_id}.reconciled.json"
                ).exists()
            )

            replayed = await output_store.reconcile_unknown(delivered_receipt)
            self.assertEqual(replayed.state, OutputBatchState.DELIVERED)
            self.assertEqual(
                (await artifacts.delivery_store.get(selected.delivery_id)).state,
                ArtifactDeliveryState.DELIVERED,
            )


if __name__ == "__main__":
    unittest.main()
