import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.artifacts import ArtifactConfigType, create_artifact_services
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
from src.interaction.output_service import OutputBatchAssembler
from src.interaction.output_store import FileSystemOutputBatchStore
from src.storage import create_storage_services
from src.storage.config import StorageConfigType


class OutputSemanticManifestBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        storage_config = StorageConfigType(root_dir=str(self.root))
        storage = create_storage_services(storage_config)
        artifacts = create_artifact_services(
            storage_config=storage_config,
            artifact_config=ArtifactConfigType(),
            content_store=storage.content_store,
        )
        self.snapshot = build_default_capability_registry().resolve(
            build_telegram_capability_declaration(),
            client_type="telegram",
            client_instance_id="bot-1",
        )
        self.assembler = OutputBatchAssembler(
            config=OutputRuntimeConfig(max_metadata_bytes=1024),
            delivery_store=artifacts.delivery_store,
            output_store=FileSystemOutputBatchStore(self.root),
        )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_large_typed_vcard_cannot_bypass_metadata_budget(self):
        result = AgentResult(
            content="",
            status=AgentStatus.DONE,
            session_id="session-1",
            cycle_id="cycle-1",
            semantic_outputs=[
                {
                    "type": "contact_output",
                    "index": 0,
                    "phone_number": "+10000000000",
                    "first_name": "Contact",
                    "vcard": "X" * 5000,
                }
            ],
        )
        with self.assertRaisesRegex(
            InteractionValidationError,
            "semantic manifest exceeds metadata policy",
        ):
            await self.assembler.assemble_final(
                result=result,
                input_batch=self._input_batch(),
            )

    def _input_batch(self) -> CommittedInputBatch:
        return CommittedInputBatch(
            input_batch_id=new_input_batch_id(),
            session_id="session-1",
            client_type=ClientType.TELEGRAM,
            sequence_number=1,
            source_event_ids=[new_ingress_event_id()],
            admission_mode="auto",
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="100",
            ),
            locale="en",
            capability_snapshot=self.snapshot,
            committed_at=datetime.now(timezone.utc),
            commit_reason="test",
            content_fingerprint="sha256:" + ("a" * 64),
        )


if __name__ == "__main__":
    unittest.main()
