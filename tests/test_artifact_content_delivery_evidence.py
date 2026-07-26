import tempfile
import unittest
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
    ClientCapabilityDeclaration,
    build_default_capability_registry,
    build_telegram_capability_declaration,
)
from src.interaction.ids import new_output_part_id
from src.interaction.output_completion import OutputDeliveryCompletionService
from src.interaction.output_models import (
    ArtifactContentReceiptState,
    ArtifactOutputPart,
    ImageOutputPart,
    OutputBatchKind,
    OutputBatchState,
    OutputDeliveryReceiptState,
    OutputPartReceiptState,
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


class ArtifactContentDeliveryEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.storage_config = StorageConfigType(root_dir=str(self.root))
        storage = create_storage_services(self.storage_config)
        self.content_store = storage.content_store
        self.artifacts = create_artifact_services(
            storage_config=self.storage_config,
            artifact_config=ArtifactConfigType(),
            content_store=self.content_store,
        )
        self.output_store = FileSystemOutputBatchStore(self.root)
        self.completion = OutputDeliveryCompletionService(
            output_store=self.output_store,
            artifact_delivery_store=self.artifacts.delivery_store,
        )
        self.registry = build_default_capability_registry()
        self.route = ClientResponseRoute(
            route_type="telegram",
            conversation_id="chat-1",
        )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_text_fallback_delivers_part_but_not_artifact_bytes(self):
        artifact, selected = await self._create_text_artifact()
        snapshot = self.registry.resolve(
            ClientCapabilityDeclaration(
                capability_contract_version=1,
                features=("output.text",),
                limits={"transport.telegram.output.text.max_chars": 4096},
            ),
            client_type="telegram",
            client_instance_id="bot-1",
        )
        part = ArtifactOutputPart(
            part_id=new_output_part_id(),
            index=0,
            artifact_id=artifact.artifact_id,
            delivery_id=selected.delivery_id,
            filename=selected.filename,
            mime_type=selected.mime_type,
            size_bytes=selected.size_bytes,
        )
        batch = await self._commit_batch(snapshot=snapshot, part=part)
        receipt = await self._execute(batch)
        self.assertEqual(receipt.state, OutputDeliveryReceiptState.DELIVERED)
        self.assertEqual(
            receipt.part_receipts[0].artifact_content_state,
            ArtifactContentReceiptState.NOT_DELIVERED,
        )

        completed = await self.completion.complete(receipt)
        self.assertEqual(completed.state, OutputBatchState.DELIVERED)
        delivery = await self.artifacts.delivery_store.get(selected.delivery_id)
        self.assertEqual(delivery.state, ArtifactDeliveryState.FAILED)
        self.assertEqual(delivery.last_error, "artifact_content_not_delivered")
        self.assertEqual(
            delivery.receipt["artifact_content_state"],
            "not_delivered",
        )

    async def test_media_bytes_remain_delivered_when_caption_is_partial(self):
        artifact, selected = await self._create_image_artifact()
        await self.artifacts.delivery_service.claim(selected.delivery_id)
        base_declaration = build_telegram_capability_declaration()
        declaration = base_declaration.model_copy(update={
            "limits": {
                **base_declaration.limits,
                "transport.telegram.output.text.max_chars": 4,
                "transport.telegram.output.caption.max_chars": 5,
            }
        })
        snapshot = self.registry.resolve(
            declaration,
            client_type="telegram",
            client_instance_id="bot-1",
        )
        part = ImageOutputPart(
            part_id=new_output_part_id(),
            index=0,
            artifact_id=artifact.artifact_id,
            delivery_id=selected.delivery_id,
            filename=selected.filename,
            mime_type=selected.mime_type,
            size_bytes=selected.size_bytes,
            caption="abcdefghij",
        )
        batch = await self._commit_batch(snapshot=snapshot, part=part)
        bot = FakeTelegramBot()
        bot.queue("send_message", BadRequest("caption rejected"))
        receipt = await self._execute(batch, bot=bot)
        self.assertEqual(
            receipt.part_receipts[0].state,
            OutputPartReceiptState.PARTIALLY_DELIVERED,
        )
        self.assertEqual(
            receipt.part_receipts[0].artifact_content_state,
            ArtifactContentReceiptState.DELIVERED,
        )

        completed = await self.completion.complete(receipt)
        self.assertEqual(completed.state, OutputBatchState.PARTIALLY_DELIVERED)
        delivery = await self.artifacts.delivery_store.get(selected.delivery_id)
        self.assertEqual(delivery.state, ArtifactDeliveryState.DELIVERED)
        self.assertEqual(
            delivery.receipt["artifact_content_state"],
            "delivered",
        )

    async def _commit_batch(self, *, snapshot, part):
        batch = build_ready_output_batch(
            session_id="session-1",
            cycle_id="cycle-1",
            sequence_number=1,
            kind=OutputBatchKind.FINAL,
            response_route=self.route,
            locale="ru",
            capability_snapshot=snapshot,
            parts=(part,),
        )
        batch, _ = await self.output_store.commit(batch)
        _, attempt_id = await self.output_store.claim_delivery(
            batch.output_batch_id
        )
        self.attempt_id = attempt_id
        return batch

    async def _execute(self, batch, *, bot=None):
        return await TelegramOutputPlanExecutor().execute(
            batch=batch,
            plan=CapabilityOutputRenderer().plan(batch),
            attempt_id=self.attempt_id,
            context=TelegramExecutionContext(
                bot=bot or FakeTelegramBot(),
                gateway=FakeTelegramGateway(),
                session_id="session-1",
                chat_id=1,
            ),
        )

    async def _create_text_artifact(self):
        artifact = await self.artifacts.artifact_service.create_text(
            session_id="session-1",
            cycle_id="cycle-1",
            filename="result.md",
            text="result",
            format_id="markdown",
            purpose=ArtifactPurpose.DELIVERABLE,
            provenance=ArtifactProvenance(
                origin="agent_created",
                creator="agent",
                operation="fallback_test",
            ),
        )
        selected = await self.artifacts.delivery_service.select(
            artifact_id=artifact.artifact_id,
            access=ArtifactAccessContext(
                session_id="session-1",
                cycle_id="cycle-1",
                allowed_artifact_ids=[artifact.artifact_id],
            ),
            client_type="telegram",
        )
        return artifact, selected

    async def _create_image_artifact(self):
        content = await self.content_store.save_content(
            b"\x89PNG\r\n\x1a\nimage",
            source_type="artifact_test",
            source_name="image.png",
            mime_type="image/png",
            cycle_id="cycle-1",
            metadata={"artifact_format_id": "png"},
        )
        _, version = await self.artifacts.artifact_store.create_lineage(
            session_id="session-1",
            cycle_id="cycle-1",
            content_id=content.content_id,
            filename="image.png",
            format_id="png",
            detected_mime_type="image/png",
            declared_mime_type="image/png",
            purpose=ArtifactPurpose.DELIVERABLE,
            provenance=ArtifactProvenance(
                origin="agent_created",
                creator="agent",
                operation="caption_test",
            ),
        )
        access = ArtifactAccessContext(
            session_id="session-1",
            cycle_id="cycle-1",
            allowed_artifact_ids=[version.artifact_id],
        )
        artifact = await self.artifacts.artifact_service.get_artifact(
            version.artifact_id,
            access=access,
        )
        selected = await self.artifacts.delivery_service.select(
            artifact_id=artifact.artifact_id,
            access=access,
            client_type="telegram",
        )
        return artifact, selected


if __name__ == "__main__":
    unittest.main()
