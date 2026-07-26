import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError
from telegram.error import BadRequest

from src.artifacts import (
    ArtifactAccessContext,
    ArtifactConfigType,
    ArtifactProvenance,
    ArtifactPurpose,
    create_artifact_services,
)
from src.artifacts.models import ArtifactDeliveryState
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
    build_web_capability_declaration,
)
from src.interaction.config import OutputRuntimeConfig
from src.interaction.errors import InteractionValidationError
from src.interaction.ids import (
    new_output_attempt_id,
    new_output_delivery_group_id,
    new_output_part_id,
)
from src.interaction.output_completion import OutputDeliveryCompletionService
from src.interaction.output_models import (
    ArtifactOutputPart,
    LocationOutputPart,
    OutputBatchKind,
    OutputBatchState,
    OutputDeliveryGroup,
    OutputDeliveryPlan,
    OutputDeliveryReceipt,
    OutputDeliveryReceiptState,
    OutputPartReceipt,
    OutputPartReceiptState,
    StatusOutputPart,
    TextOutputPart,
    TransportOperationKind,
)
from src.interaction.output_service import OutputBatchAssembler
from src.interaction.output_store import (
    FileSystemOutputBatchStore,
    build_ready_output_batch,
)
from src.interaction.rendering import CapabilityOutputRenderer
from src.localization.models import LocalizationMessage
from src.servers.telegram.output_plan_executor import (
    TelegramExecutionContext,
    TelegramOutputPlanExecutor,
)
from src.storage import create_storage_services
from src.storage.config import StorageConfigType
from tests.telegram_fakes import FakeTelegramBot, FakeTelegramGateway


UTC = timezone.utc


class OutputRuntimeHardeningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.storage_config = StorageConfigType(root_dir=str(self.root))
        storage = create_storage_services(self.storage_config)
        self.artifacts = create_artifact_services(
            storage_config=self.storage_config,
            artifact_config=ArtifactConfigType(),
            content_store=storage.content_store,
        )
        self.output_store = FileSystemOutputBatchStore(self.root)
        self.completion = OutputDeliveryCompletionService(
            output_store=self.output_store,
            artifact_delivery_store=self.artifacts.delivery_store,
        )
        registry = build_default_capability_registry()
        self.telegram_snapshot = registry.resolve(
            build_telegram_capability_declaration(),
            client_type="telegram",
            client_instance_id="bot-1",
        )
        self.web_snapshot = registry.resolve(
            build_web_capability_declaration(),
            client_type="web",
            client_instance_id="web-1",
        )
        self.telegram_route = ClientResponseRoute(
            route_type="telegram",
            conversation_id="chat-1",
        )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_semantic_artifact_intents_cannot_reorder_selection(self):
        created, selected = await self._create_selected(
            ["a.md", "b.md", "c.md", "d.md"]
        )
        assembler = self._assembler()
        result = AgentResult(
            content="Final",
            status=AgentStatus.DONE,
            session_id="session-1",
            cycle_id="cycle-1",
            semantic_outputs=[
                self._artifact_intent(created[3], selected[3], index=0),
                self._artifact_intent(created[0], selected[0], index=1),
            ],
        )
        batch = await assembler.assemble_final(
            result=result,
            input_batch=self._input_batch(),
        )
        artifact_parts = [
            part for part in batch.parts if isinstance(part, ArtifactOutputPart)
        ]
        self.assertEqual(
            [part.delivery_id for part in artifact_parts],
            [item.delivery_id for item in selected],
        )
        self.assertEqual(
            [part.metadata["selection_index"] for part in artifact_parts],
            [0, 1, 2, 3],
        )

    async def test_duplicate_semantic_delivery_intent_is_rejected(self):
        created, selected = await self._create_selected(["result.md"])
        intent = self._artifact_intent(created[0], selected[0], index=0)
        duplicate = {
            **intent,
            "part_id": new_output_part_id(),
            "index": 1,
        }
        with self.assertRaises(InteractionValidationError):
            await self._assembler().assemble_final(
                result=AgentResult(
                    content="Final",
                    status=AgentStatus.DONE,
                    session_id="session-1",
                    cycle_id="cycle-1",
                    semantic_outputs=[intent, duplicate],
                ),
                input_batch=self._input_batch(),
            )

    async def test_stale_selected_artifact_recovers_without_reconciliation_conflict(self):
        _, selected = await self._create_selected(["result.md"])
        batch = self._output_batch(
            parts=(
                TextOutputPart(
                    part_id=new_output_part_id(),
                    index=0,
                    text="Result",
                ),
                self._artifact_part(selected[0], index=1),
            )
        )
        batch, _ = await self.output_store.commit(batch)
        now = datetime.now(UTC)
        _, attempt_id = await self.output_store.claim_delivery(
            batch.output_batch_id,
            now=now - timedelta(seconds=10),
        )

        recovered = await self.output_store.reconcile_stale_claims(
            timeout_seconds=5,
            now=now,
        )
        self.assertEqual(recovered[0].state, OutputBatchState.UNKNOWN)
        delivery = await self.artifacts.delivery_store.get(selected[0].delivery_id)
        self.assertEqual(delivery.state, ArtifactDeliveryState.FAILED)
        self.assertEqual(
            delivery.last_error,
            "transport_not_started_before_recovery",
        )

        resolved = OutputDeliveryReceipt(
            output_batch_id=batch.output_batch_id,
            attempt_id=attempt_id,
            state=OutputDeliveryReceiptState.PARTIALLY_DELIVERED,
            part_receipts=(
                OutputPartReceipt(
                    part_id=batch.parts[0].part_id,
                    index=0,
                    state=OutputPartReceiptState.DELIVERED,
                    client_message_ids=("901",),
                    delivered_at=now,
                ),
                OutputPartReceipt(
                    part_id=batch.parts[1].part_id,
                    index=1,
                    state=OutputPartReceiptState.FAILED,
                    delivery_id=selected[0].delivery_id,
                    error_category="transport_not_started_before_recovery",
                ),
            ),
            started_at=now,
            completed_at=now,
        )
        reconciled = await self.output_store.reconcile_unknown(resolved)
        self.assertEqual(
            reconciled.state,
            OutputBatchState.PARTIALLY_DELIVERED,
        )
        self.assertEqual(
            (await self.artifacts.delivery_store.get(selected[0].delivery_id)).state,
            ArtifactDeliveryState.FAILED,
        )

    async def test_stale_delivering_artifact_reconciles_to_delivered(self):
        _, selected = await self._create_selected(["result.md"])
        await self.artifacts.delivery_service.claim(selected[0].delivery_id)
        batch = self._output_batch(
            parts=(self._artifact_part(selected[0], index=0),)
        )
        batch, _ = await self.output_store.commit(batch)
        now = datetime.now(UTC)
        _, attempt_id = await self.output_store.claim_delivery(
            batch.output_batch_id,
            now=now - timedelta(seconds=10),
        )
        recovered = await self.output_store.reconcile_stale_claims(
            timeout_seconds=5,
            now=now,
        )
        self.assertEqual(recovered[0].state, OutputBatchState.UNKNOWN)
        self.assertEqual(
            (await self.artifacts.delivery_store.get(selected[0].delivery_id)).state,
            ArtifactDeliveryState.UNKNOWN,
        )

        resolved = OutputDeliveryReceipt(
            output_batch_id=batch.output_batch_id,
            attempt_id=attempt_id,
            state=OutputDeliveryReceiptState.DELIVERED,
            part_receipts=(
                OutputPartReceipt(
                    part_id=batch.parts[0].part_id,
                    index=0,
                    state=OutputPartReceiptState.DELIVERED,
                    delivery_id=selected[0].delivery_id,
                    client_message_ids=("902",),
                    delivered_at=now,
                ),
            ),
            started_at=now,
            completed_at=now,
        )
        reconciled = await self.output_store.reconcile_unknown(resolved)
        self.assertEqual(reconciled.state, OutputBatchState.DELIVERED)
        self.assertEqual(
            (await self.artifacts.delivery_store.get(selected[0].delivery_id)).state,
            ArtifactDeliveryState.DELIVERED,
        )

    async def test_completion_receipt_uses_committed_client_identity(self):
        _, selected = await self._create_selected(
            ["result.md"],
            client_type="web",
        )
        await self.artifacts.delivery_service.claim(selected[0].delivery_id)
        batch = self._output_batch(
            parts=(self._artifact_part(selected[0], index=0),),
            snapshot=self.web_snapshot,
            route=ClientResponseRoute(
                route_type="web",
                conversation_id="conversation-1",
            ),
        )
        batch, _ = await self.output_store.commit(batch)
        _, attempt_id = await self.output_store.claim_delivery(batch.output_batch_id)
        now = datetime.now(UTC)
        await self.completion.complete(OutputDeliveryReceipt(
            output_batch_id=batch.output_batch_id,
            attempt_id=attempt_id,
            state=OutputDeliveryReceiptState.DELIVERED,
            part_receipts=(
                OutputPartReceipt(
                    part_id=batch.parts[0].part_id,
                    index=0,
                    state=OutputPartReceiptState.DELIVERED,
                    delivery_id=selected[0].delivery_id,
                    client_message_ids=("web-message-1",),
                    delivered_at=now,
                ),
            ),
            started_at=now,
            completed_at=now,
        ))
        record = await self.artifacts.delivery_store.get(selected[0].delivery_id)
        self.assertEqual(record.receipt["provider"], "web")
        self.assertEqual(record.receipt["client_instance_id"], "web-1")

    async def test_executor_rejects_incomplete_or_forged_plan(self):
        batch = self._output_batch(parts=(
            TextOutputPart(
                part_id=new_output_part_id(),
                index=0,
                text="A",
            ),
            TextOutputPart(
                part_id=new_output_part_id(),
                index=1,
                text="B",
            ),
        ))
        incomplete = OutputDeliveryPlan(
            output_batch_id=batch.output_batch_id,
            groups=(
                OutputDeliveryGroup(
                    group_id=new_output_delivery_group_id(),
                    index=0,
                    operation_kind=TransportOperationKind.TEXT,
                    part_ids=(batch.parts[1].part_id,),
                    required=True,
                    rendered_text="B",
                ),
            ),
            created_at=batch.created_at,
        )
        with self.assertRaises(ValueError):
            await self._execute(batch, incomplete)

        forged_required = CapabilityOutputRenderer().plan(batch).model_copy(
            update={
                "groups": (
                    CapabilityOutputRenderer().plan(batch).groups[0].model_copy(
                        update={"required": False}
                    ),
                    CapabilityOutputRenderer().plan(batch).groups[1],
                )
            }
        )
        with self.assertRaises(ValueError):
            await self._execute(batch, forged_required)

    async def test_reply_anchor_survives_pre_send_failure(self):
        optional = LocationOutputPart(
            part_id=new_output_part_id(),
            index=0,
            required=False,
            latitude=1,
            longitude=2,
        )
        text = TextOutputPart(
            part_id=new_output_part_id(),
            index=1,
            text="Delivered",
        )
        batch = self._output_batch(parts=(optional, text))
        plan = OutputDeliveryPlan(
            output_batch_id=batch.output_batch_id,
            groups=(
                OutputDeliveryGroup(
                    group_id=new_output_delivery_group_id(),
                    index=0,
                    operation_kind=TransportOperationKind.UNSUPPORTED,
                    part_ids=(optional.part_id,),
                    required=False,
                ),
                OutputDeliveryGroup(
                    group_id=new_output_delivery_group_id(),
                    index=1,
                    operation_kind=TransportOperationKind.TEXT,
                    part_ids=(text.part_id,),
                    required=True,
                    rendered_text="Delivered",
                ),
            ),
            created_at=batch.created_at,
        )
        bot, receipt = await self._execute(batch, plan, reply_to_message_id=77)
        self.assertEqual(receipt.state, OutputDeliveryReceiptState.DELIVERED)
        self.assertEqual(bot.calls[-1][0], "send_message")
        self.assertEqual(bot.calls[-1][1]["reply_to_message_id"], 77)

    async def test_status_not_modified_is_idempotent_delivery(self):
        part = StatusOutputPart(
            part_id=new_output_part_id(),
            index=0,
            message=LocalizationMessage(message_key="output.done"),
        )
        batch = self._output_batch(parts=(part,))
        plan = CapabilityOutputRenderer().plan(batch)
        bot = FakeTelegramBot()
        bot.queue("edit_message_text", BadRequest("Message is not modified"))
        _, receipt = await self._execute(
            batch,
            plan,
            bot=bot,
            status_message_id=88,
        )
        self.assertEqual(
            receipt.part_receipts[0].state,
            OutputPartReceiptState.DELIVERED,
        )
        self.assertEqual(receipt.part_receipts[0].client_message_ids, ("88",))

    async def test_media_group_mismatch_preserves_available_message_ids(self):
        parts = (
            ArtifactOutputPart(
                part_id=new_output_part_id(),
                index=0,
                artifact_id=self._fake_artifact_id(1),
                delivery_id=self._fake_delivery_id(1),
                filename="a.txt",
                mime_type="text/plain",
                size_bytes=1,
            ),
            ArtifactOutputPart(
                part_id=new_output_part_id(),
                index=1,
                artifact_id=self._fake_artifact_id(2),
                delivery_id=self._fake_delivery_id(2),
                filename="b.txt",
                mime_type="text/plain",
                size_bytes=1,
            ),
        )
        batch = self._output_batch(parts=parts)
        plan = CapabilityOutputRenderer().plan(batch)
        bot = FakeTelegramBot()
        bot.queue("send_media_group", 1)
        _, receipt = await self._execute(batch, plan, bot=bot)
        self.assertEqual(receipt.state, OutputDeliveryReceiptState.UNKNOWN)
        self.assertEqual(len(receipt.part_receipts[0].client_message_ids), 1)
        self.assertEqual(receipt.part_receipts[1].client_message_ids, ())

    def test_final_output_batch_requires_nonempty_required_manifest(self):
        with self.assertRaises(ValidationError):
            self._output_batch(parts=())
        with self.assertRaises(ValidationError):
            self._output_batch(parts=(
                TextOutputPart(
                    part_id=new_output_part_id(),
                    index=0,
                    required=False,
                    text="Optional",
                ),
            ))

    async def _create_selected(self, filenames, *, client_type="telegram"):
        access = ArtifactAccessContext(
            session_id="session-1",
            cycle_id="cycle-1",
            allowed_artifact_ids=[],
        )
        created = []
        for filename in filenames:
            item = await self.artifacts.artifact_service.create_text(
                session_id="session-1",
                cycle_id="cycle-1",
                filename=filename,
                text=filename,
                format_id="markdown",
                purpose=ArtifactPurpose.DELIVERABLE,
                provenance=ArtifactProvenance(
                    origin="agent_created",
                    creator="agent",
                    operation="hardening_test",
                ),
            )
            access.allowed_artifact_ids.append(item.artifact_id)
            created.append(item)
        selected = await self.artifacts.delivery_service.select_many(
            artifact_ids=[item.artifact_id for item in created],
            access=access,
            client_type=client_type,
        )
        return created, selected

    def _assembler(self):
        return OutputBatchAssembler(
            config=OutputRuntimeConfig(),
            delivery_store=self.artifacts.delivery_store,
            output_store=self.output_store,
        )

    def _input_batch(self):
        now = datetime.now(UTC)
        return CommittedInputBatch(
            input_batch_id=new_input_batch_id(),
            session_id="session-1",
            client_type=ClientType.TELEGRAM,
            sequence_number=1,
            source_event_ids=[new_ingress_event_id()],
            admission_mode="auto",
            response_route=self.telegram_route,
            locale="ru",
            capability_snapshot=self.telegram_snapshot,
            committed_at=now,
            commit_reason="test",
            content_fingerprint="sha256:" + ("c" * 64),
        )

    def _output_batch(self, *, parts, snapshot=None, route=None):
        return build_ready_output_batch(
            session_id="session-1",
            cycle_id="cycle-1",
            sequence_number=1,
            kind=OutputBatchKind.FINAL,
            response_route=route or self.telegram_route,
            locale="ru",
            capability_snapshot=snapshot or self.telegram_snapshot,
            parts=parts,
        )

    @staticmethod
    def _artifact_intent(artifact, delivery, *, index):
        return {
            "type": "artifact_output",
            "part_id": new_output_part_id(),
            "index": index,
            "artifact_id": artifact.artifact_id,
            "delivery_id": delivery.delivery_id,
            "filename": "untrusted-name.txt",
            "mime_type": "application/octet-stream",
            "size_bytes": 1,
        }

    @staticmethod
    def _artifact_part(delivery, *, index):
        return ArtifactOutputPart(
            part_id=new_output_part_id(),
            index=index,
            artifact_id=delivery.artifact_id,
            delivery_id=delivery.delivery_id,
            filename=delivery.filename,
            mime_type=delivery.mime_type,
            size_bytes=delivery.size_bytes,
        )

    async def _execute(
        self,
        batch,
        plan,
        *,
        bot=None,
        reply_to_message_id=None,
        status_message_id=None,
    ):
        fake_bot = bot or FakeTelegramBot()
        receipt = await TelegramOutputPlanExecutor().execute(
            batch=batch,
            plan=plan,
            attempt_id=new_output_attempt_id(),
            context=TelegramExecutionContext(
                bot=fake_bot,
                gateway=FakeTelegramGateway(),
                session_id="session-1",
                chat_id=1,
                reply_to_message_id=reply_to_message_id,
                status_message_id=status_message_id,
            ),
        )
        return fake_bot, receipt

    @staticmethod
    def _fake_artifact_id(seed):
        from src.artifacts.models import new_artifact_id

        del seed
        return new_artifact_id()

    @staticmethod
    def _fake_delivery_id(seed):
        from src.artifacts.models import new_artifact_delivery_id

        del seed
        return new_artifact_delivery_id()


if __name__ == "__main__":
    unittest.main()
