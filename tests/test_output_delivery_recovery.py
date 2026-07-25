import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

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
from src.interaction.errors import InteractionStorageError
from src.interaction.ids import new_output_part_id
from src.interaction.output_completion import (
    OutputDeliveryCompletionService,
)
from src.interaction.output_models import (
    ArtifactOutputPart,
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
from src.storage import create_storage_services
from src.storage.config import StorageConfigType


class OutputDeliveryRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_is_terminal_queryable_and_not_recoverable(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = FileSystemOutputBatchStore(Path(temporary))
            batch, _ = await store.commit(self._text_batch())
            _, attempt_id = await store.claim_delivery(
                batch.output_batch_id
            )
            now = datetime.now(timezone.utc)
            unknown = await store.complete(OutputDeliveryReceipt(
                output_batch_id=batch.output_batch_id,
                attempt_id=attempt_id,
                state=OutputDeliveryReceiptState.UNKNOWN,
                part_receipts=(
                    OutputPartReceipt(
                        part_id=batch.parts[0].part_id,
                        index=0,
                        state=OutputPartReceiptState.UNKNOWN,
                        error_category="transport_timeout_after_start",
                    ),
                ),
                started_at=now,
                completed_at=now,
            ))
            self.assertEqual(unknown.state, OutputBatchState.UNKNOWN)
            self.assertEqual(await store.list_recoverable(), [])
            self.assertEqual(
                [item.output_batch_id for item in await store.list_unknown()],
                [batch.output_batch_id],
            )

    async def test_aggregate_completion_rolls_back_every_store_on_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = StorageConfigType(root_dir=temporary)
            storage = create_storage_services(config)
            artifacts = create_artifact_services(
                storage_config=config,
                artifact_config=ArtifactConfigType(),
                content_store=storage.content_store,
            )
            artifact = await artifacts.artifact_service.create_text(
                session_id="session",
                cycle_id="cycle",
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
                    session_id="session",
                    cycle_id="cycle",
                    allowed_artifact_ids=[artifact.artifact_id],
                ),
                client_type="telegram",
            )
            await artifacts.delivery_service.claim(selected.delivery_id)

            output_store = FileSystemOutputBatchStore(Path(temporary))
            batch = build_ready_output_batch(
                session_id="session",
                cycle_id="cycle",
                sequence_number=1,
                kind=OutputBatchKind.FINAL,
                response_route=ClientResponseRoute(
                    route_type="telegram",
                    conversation_id="chat",
                ),
                locale="en",
                capability_snapshot=build_default_capability_registry().resolve(
                    build_telegram_capability_declaration(),
                    client_type="telegram",
                    client_instance_id="bot",
                ),
                parts=(
                    ArtifactOutputPart(
                        part_id=new_output_part_id(),
                        index=0,
                        artifact_id=artifact.artifact_id,
                        delivery_id=selected.delivery_id,
                        filename="result.md",
                        mime_type="text/markdown",
                        size_bytes=6,
                    ),
                ),
            )
            batch, _ = await output_store.commit(batch)
            _, attempt_id = await output_store.claim_delivery(
                batch.output_batch_id
            )
            now = datetime.now(timezone.utc)
            receipt = OutputDeliveryReceipt(
                output_batch_id=batch.output_batch_id,
                attempt_id=attempt_id,
                state=OutputDeliveryReceiptState.DELIVERED,
                part_receipts=(
                    OutputPartReceipt(
                        part_id=batch.parts[0].part_id,
                        index=0,
                        state=OutputPartReceiptState.DELIVERED,
                        delivery_id=selected.delivery_id,
                        client_message_ids=("501",),
                        delivered_at=now,
                    ),
                ),
                started_at=now,
                completed_at=now,
            )
            completion = OutputDeliveryCompletionService(
                output_store=output_store,
                artifact_delivery_store=artifacts.delivery_store,
            )
            original_write = output_store._write

            def fail_state(path, payload):
                if path.name == "state.json":
                    raise InteractionStorageError("simulated state failure")
                return original_write(path, payload)

            output_store._write = fail_state
            with self.assertRaises(InteractionStorageError):
                await completion.complete(receipt)
            output_store._write = original_write

            delivery = await artifacts.delivery_store.get(
                selected.delivery_id
            )
            restored = await output_store.get(batch.output_batch_id)
            self.assertEqual(
                delivery.state,
                ArtifactDeliveryState.DELIVERING,
            )
            self.assertEqual(restored.state, OutputBatchState.DELIVERING)
            self.assertFalse(
                (
                    output_store.attempts
                    / f"{attempt_id}.json"
                ).exists()
            )

    @staticmethod
    def _text_batch():
        snapshot = build_default_capability_registry().resolve(
            build_telegram_capability_declaration(),
            client_type="telegram",
            client_instance_id="bot",
        )
        return build_ready_output_batch(
            session_id="session",
            cycle_id="cycle",
            sequence_number=1,
            kind=OutputBatchKind.FINAL,
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="chat",
            ),
            locale="en",
            capability_snapshot=snapshot,
            parts=(
                TextOutputPart(
                    part_id=new_output_part_id(),
                    index=0,
                    text="result",
                ),
            ),
        )
