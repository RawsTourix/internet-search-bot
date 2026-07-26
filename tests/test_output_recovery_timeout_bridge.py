import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.artifacts import (
    ArtifactAccessContext,
    ArtifactConfigType,
    ArtifactProvenance,
    ArtifactPurpose,
    create_artifact_services,
    recover_stale_delivery_claims,
)
from src.artifacts.models import ArtifactDeliveryState
from src.ingress.models import ClientResponseRoute
from src.interaction.capabilities import (
    build_default_capability_registry,
    build_telegram_capability_declaration,
)
from src.interaction.ids import new_output_part_id
from src.interaction.output_completion import OutputDeliveryCompletionService
from src.interaction.output_models import (
    ArtifactOutputPart,
    OutputBatchKind,
    OutputBatchState,
)
from src.interaction.output_store import (
    FileSystemOutputBatchStore,
    build_ready_output_batch,
)
from src.storage import create_storage_services
from src.storage.config import StorageConfigType


UTC = timezone.utc


class OutputRecoveryTimeoutBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_artifact_recovery_marker_forces_output_reconciliation(self):
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
                    operation="timeout_bridge_test",
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
            await artifacts.delivery_service.claim(selected.delivery_id)

            registry = build_default_capability_registry()
            snapshot = registry.resolve(
                build_telegram_capability_declaration(),
                client_type="telegram",
                client_instance_id="bot-1",
            )
            output_store = FileSystemOutputBatchStore(Path(temporary))
            OutputDeliveryCompletionService(
                output_store=output_store,
                artifact_delivery_store=artifacts.delivery_store,
            )
            batch = build_ready_output_batch(
                session_id="session-1",
                cycle_id="cycle-1",
                sequence_number=1,
                kind=OutputBatchKind.FINAL,
                response_route=ClientResponseRoute(
                    route_type="telegram",
                    conversation_id="chat-1",
                ),
                locale="ru",
                capability_snapshot=snapshot,
                parts=(
                    ArtifactOutputPart(
                        part_id=new_output_part_id(),
                        index=0,
                        artifact_id=selected.artifact_id,
                        delivery_id=selected.delivery_id,
                        filename=selected.filename,
                        mime_type=selected.mime_type,
                        size_bytes=selected.size_bytes,
                    ),
                ),
            )
            batch, _ = await output_store.commit(batch)
            now = datetime.now(UTC)
            await output_store.claim_delivery(batch.output_batch_id, now=now)

            recovered_artifacts = await recover_stale_delivery_claims(
                artifacts.delivery_store,
                claim_timeout_seconds=1,
                now=now + timedelta(seconds=2),
            )
            self.assertEqual(len(recovered_artifacts), 1)
            self.assertEqual(
                (await artifacts.delivery_store.get(selected.delivery_id)).state,
                ArtifactDeliveryState.UNKNOWN,
            )

            reconciled = await output_store.reconcile_stale_claims(
                timeout_seconds=900,
                now=now + timedelta(seconds=2),
            )
            self.assertEqual(len(reconciled), 1)
            self.assertEqual(reconciled[0].state, OutputBatchState.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
