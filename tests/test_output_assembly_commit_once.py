import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from src.artifacts import (
    ArtifactAccessContext,
    ArtifactConfigType,
    ArtifactDeliveryState,
    ArtifactProvenance,
    ArtifactPurpose,
    create_artifact_services,
)
from src.artifacts.errors import ArtifactDeliveryError
from src.api.artifact_transport import (
    ArtifactTransportFacade,
    DeliveryReceiptRequest,
)
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
from src.interaction.output_completion import OutputDeliveryCompletionService
from src.interaction.output_models import (
    ArtifactOutputPart,
    OutputBatchKind,
    OutputBatchState,
    OutputDeliveryReceipt,
    OutputDeliveryReceiptState,
    OutputPartReceipt,
    OutputPartReceiptState,
)
from src.interaction.output_service import OutputBatchAssembler
from src.interaction.output_store import FileSystemOutputBatchStore
from src.storage import create_storage_services
from src.storage.config import StorageConfigType


UTC = timezone.utc


class OutputAssemblyCommitOnceTests(unittest.IsolatedAsyncioTestCase):
    async def test_binding_failure_rolls_back_new_output_commit(self):
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
                cycle_id="cycle-1",
                filename="result.md",
                text="result",
                format_id="markdown",
                purpose=ArtifactPurpose.DELIVERABLE,
                provenance=ArtifactProvenance(
                    origin="agent_created",
                    creator="agent",
                    operation="rollback_test",
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
            output_store = FileSystemOutputBatchStore(Path(temporary))
            assembler = OutputBatchAssembler(
                config=OutputRuntimeConfig(),
                delivery_store=artifacts.delivery_store,
                output_store=output_store,
            )
            input_batch = self._input_batch()
            before = await artifacts.delivery_store.get(selected.delivery_id)

            with patch.object(
                artifacts.delivery_store,
                "_bind_output_batch_sync",
                side_effect=ArtifactDeliveryError("injected binding failure"),
            ):
                with self.assertRaisesRegex(
                    ArtifactDeliveryError,
                    "injected binding failure",
                ):
                    await assembler.assemble_final(
                        result=AgentResult(
                            content="Final text",
                            status=AgentStatus.DONE,
                            session_id="session-1",
                            cycle_id="cycle-1",
                        ),
                        input_batch=input_batch,
                    )

            self.assertIsNone(await output_store.get_for_cycle(
                session_id="session-1",
                cycle_id="cycle-1",
                kind=OutputBatchKind.FINAL,
            ))
            self.assertFalse(any(output_store.records.iterdir()))
            self.assertFalse(any(output_store.cycle_index.iterdir()))
            self.assertEqual(
                await artifacts.delivery_store.get(selected.delivery_id),
                before,
            )

    async def test_terminal_output_is_reused_after_delivery_state_changes(self):
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
                cycle_id="cycle-1",
                filename="result.md",
                text="result",
                format_id="markdown",
                purpose=ArtifactPurpose.DELIVERABLE,
                provenance=ArtifactProvenance(
                    origin="agent_created",
                    creator="agent",
                    operation="commit_once_test",
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
            output_store = FileSystemOutputBatchStore(Path(temporary))
            completion = OutputDeliveryCompletionService(
                output_store=output_store,
                artifact_delivery_store=artifacts.delivery_store,
            )
            assembler = OutputBatchAssembler(
                config=OutputRuntimeConfig(),
                delivery_store=artifacts.delivery_store,
                output_store=output_store,
            )
            input_batch = self._input_batch()
            result = AgentResult(
                content="Final text",
                status=AgentStatus.DONE,
                session_id="session-1",
                cycle_id="cycle-1",
            )
            batch = await assembler.assemble_final(
                result=result,
                input_batch=input_batch,
            )
            bound = await artifacts.delivery_store.get(selected.delivery_id)
            self.assertEqual(bound.output_batch_id, batch.output_batch_id)
            self.assertEqual(bound.input_batch_id, input_batch.input_batch_id)
            self.assertEqual(
                bound.client_instance_id,
                input_batch.capability_snapshot.client_instance_id,
            )
            artifact_part = next(
                part for part in batch.parts if isinstance(part, ArtifactOutputPart)
            )
            legacy_facade = ArtifactTransportFacade(
                api=type("Api", (), {"artifact_services": artifacts})(),
                message_processor=object(),
            )
            with self.assertRaisesRegex(
                ArtifactDeliveryError,
                "aggregate OutputBatch receipt",
            ):
                await legacy_facade.complete_delivery(
                    selected.delivery_id,
                    DeliveryReceiptRequest(
                        session_id="session-1",
                        client_type=ClientType.TELEGRAM,
                        receipt={"message_id": "legacy"},
                    ),
                )
            self.assertEqual(
                (await output_store.get(batch.output_batch_id)).state,
                OutputBatchState.READY,
            )
            self.assertEqual(
                (await artifacts.delivery_store.get(selected.delivery_id)).state,
                ArtifactDeliveryState.SELECTED,
            )
            await artifacts.delivery_service.claim(selected.delivery_id)
            _, attempt_id = await output_store.claim_delivery(batch.output_batch_id)
            now = datetime.now(UTC)
            receipts = []
            for part in batch.parts:
                receipts.append(OutputPartReceipt(
                    part_id=part.part_id,
                    index=part.index,
                    required=part.required,
                    state=OutputPartReceiptState.DELIVERED,
                    delivery_id=getattr(part, "delivery_id", None),
                    client_message_ids=(f"message-{part.index}",),
                    delivered_at=now,
                ))
            receipt = OutputDeliveryReceipt(
                output_batch_id=batch.output_batch_id,
                attempt_id=attempt_id,
                state=OutputDeliveryReceiptState.DELIVERED,
                part_receipts=tuple(receipts),
                started_at=now,
                completed_at=now,
            )
            completed = await completion.complete(receipt)
            self.assertEqual(completed.state, OutputBatchState.DELIVERED)
            delivered_record = await artifacts.delivery_store.get(
                selected.delivery_id
            )
            attempts_after_delivery = delivered_record.attempt_count
            replayed_receipt = await completion.complete(receipt)
            self.assertEqual(replayed_receipt.state, OutputBatchState.DELIVERED)
            self.assertEqual(
                (await artifacts.delivery_store.get(selected.delivery_id)).attempt_count,
                attempts_after_delivery,
            )
            traces = await artifacts.trace_store.list_session("session-1")
            terminal = [
                event
                for event in traces
                if event.event_type == "artifact_delivery_succeeded"
            ]
            self.assertEqual(len(terminal), 1)
            self.assertEqual(
                terminal[0].correlation.output_batch_id,
                batch.output_batch_id,
            )
            self.assertEqual(
                terminal[0].correlation.input_batch_id,
                input_batch.input_batch_id,
            )
            self.assertEqual(
                terminal[0].transport.client_instance_id,
                input_batch.capability_snapshot.client_instance_id,
            )

            replay = await assembler.assemble_final(
                result=AgentResult(
                    content="Final text",
                    status=AgentStatus.DONE,
                    session_id="session-1",
                    cycle_id="cycle-1",
                ),
                input_batch=input_batch,
            )
            self.assertEqual(replay.output_batch_id, batch.output_batch_id)
            self.assertEqual(replay.state, OutputBatchState.DELIVERED)
            self.assertEqual(
                next(
                    part.delivery_id
                    for part in replay.parts
                    if isinstance(part, ArtifactOutputPart)
                ),
                artifact_part.delivery_id,
            )

    async def test_empty_uncommitted_final_result_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = StorageConfigType(root_dir=temporary)
            storage = create_storage_services(config)
            artifacts = create_artifact_services(
                storage_config=config,
                artifact_config=ArtifactConfigType(),
                content_store=storage.content_store,
            )
            assembler = OutputBatchAssembler(
                config=OutputRuntimeConfig(),
                delivery_store=artifacts.delivery_store,
                output_store=FileSystemOutputBatchStore(Path(temporary)),
            )
            with self.assertRaises(InteractionValidationError):
                await assembler.assemble_final(
                    result=AgentResult(
                        content="",
                        status=AgentStatus.DONE,
                        session_id="session-empty",
                        cycle_id="cycle-empty",
                    ),
                    input_batch=self._input_batch(
                        session_id="session-empty",
                        event_suffix="e",
                    ),
                )

    @staticmethod
    def _input_batch(
        *,
        session_id="session-1",
        event_suffix="a",
    ):
        registry = build_default_capability_registry()
        snapshot = registry.resolve(
            build_telegram_capability_declaration(),
            client_type="telegram",
            client_instance_id="bot-1",
        )
        now = datetime.now(UTC)
        return CommittedInputBatch(
            input_batch_id=new_input_batch_id(),
            session_id=session_id,
            client_type=ClientType.TELEGRAM,
            sequence_number=1,
            source_event_ids=[new_ingress_event_id()],
            admission_mode="auto",
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="chat-1",
            ),
            locale="ru",
            capability_snapshot=snapshot,
            committed_at=now,
            commit_reason=f"test-{event_suffix}",
            content_fingerprint="sha256:" + (event_suffix * 64),
        )


if __name__ == "__main__":
    unittest.main()
