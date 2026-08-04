import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.artifacts import (
    ArtifactAccessContext,
    ArtifactConfigType,
    ArtifactProvenance,
    ArtifactPurpose,
    create_artifact_services,
)
from src.ingress.models import ClientResponseRoute, new_input_batch_id
from src.interaction.capabilities import (
    build_default_capability_registry,
    build_web_capability_declaration,
)
from src.interaction.ids import new_output_part_id
from src.interaction.output_models import (
    ArtifactOutputPart,
    OutputBatchKind,
    TextOutputPart,
)
from src.interaction.output_startup_recovery import (
    reconcile_unclaimable_legacy_ready,
)
from src.interaction.output_store import (
    FileSystemOutputBatchStore,
    build_ready_output_batch,
)
from src.storage import create_storage_services
from src.storage.config import StorageConfigType


class OutputOwnershipStartupRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_web_smoke_ready_batch_is_cancelled(self):
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = build_default_capability_registry().resolve(
                build_web_capability_declaration(),
                client_type="web",
                client_instance_id="web-artifact-smoke",
            )
            store = FileSystemOutputBatchStore(Path(temporary))
            batch = build_ready_output_batch(
                session_id="web:conversation:smoke-test",
                cycle_id="cycle-smoke-test",
                sequence_number=1,
                kind=OutputBatchKind.FINAL,
                response_route=ClientResponseRoute(
                    route_type="web",
                    conversation_id="smoke-test",
                    metadata={"smoke_test": True},
                ),
                locale="ru",
                capability_snapshot=snapshot,
                parts=(
                    TextOutputPart(
                        part_id=new_output_part_id(),
                        index=0,
                        text="smoke result",
                    ),
                ),
            )
            await store.commit(batch)

            report = await reconcile_unclaimable_legacy_ready(store)

            self.assertEqual(
                report.cancelled_test_output_batch_ids,
                (batch.output_batch_id,),
            )
            self.assertEqual((await store.get(batch.output_batch_id)).state.value, "cancelled")
            self.assertEqual(report.remaining_ready, ())

    async def test_partially_bound_ready_delivery_is_completed_idempotently(self):
        with tempfile.TemporaryDirectory() as temporary:
            prepared = await self._write_unbound_ready(
                temporary,
                cycle_id="cycle-partial-binding",
                include_input_batch_id=True,
            )
            output_store, artifacts = self._reopen(temporary)
            current = await artifacts.delivery_store.get(
                prepared.delivery_id
            )
            partial = current.model_copy(update={
                "output_batch_id": prepared.output_batch_id,
                "input_batch_id": prepared.input_batch_id,
                "client_instance_id": None,
            })
            artifacts.delivery_store._write_sync(partial, replace=True)

            output_store, artifacts = self._reopen(temporary)
            first = await reconcile_unclaimable_legacy_ready(
                output_store,
                artifacts.delivery_store,
            )
            second = await reconcile_unclaimable_legacy_ready(
                output_store,
                artifacts.delivery_store,
            )

            self.assertEqual(
                first.repaired_output_batch_ids,
                (prepared.output_batch_id,),
            )
            self.assertEqual(second.repaired_output_batch_ids, ())
            repaired = await artifacts.delivery_store.get(
                prepared.delivery_id
            )
            self.assertEqual(repaired.output_batch_id, prepared.output_batch_id)
            self.assertEqual(repaired.input_batch_id, prepared.input_batch_id)
            self.assertEqual(repaired.client_instance_id, "web-1")

    async def test_crash_window_ready_batch_is_repaired_after_stores_reopen(self):
        with tempfile.TemporaryDirectory() as temporary:
            prepared = await self._write_unbound_ready(
                temporary,
                cycle_id="cycle-crash-window",
                include_input_batch_id=True,
            )

            output_store, artifacts = self._reopen(temporary)
            before = await artifacts.delivery_store.get(prepared.delivery_id)
            self.assertIsNone(before.output_batch_id)

            report = await reconcile_unclaimable_legacy_ready(
                output_store,
                artifacts.delivery_store,
            )

            self.assertEqual(
                report.repaired_output_batch_ids,
                (prepared.output_batch_id,),
            )
            repaired = await artifacts.delivery_store.get(
                prepared.delivery_id
            )
            self.assertEqual(repaired.output_batch_id, prepared.output_batch_id)
            self.assertEqual(repaired.input_batch_id, prepared.input_batch_id)
            self.assertEqual(repaired.client_instance_id, "web-1")
            claimed, _ = await output_store.claim_delivery(
                prepared.output_batch_id
            )
            self.assertEqual(claimed.state.value, "delivering")

    async def test_real_pre_hardening_delivery_json_is_loaded_and_repaired(self):
        with tempfile.TemporaryDirectory() as temporary:
            prepared = await self._write_unbound_ready(
                temporary,
                cycle_id="cycle-pre-hardening",
                include_input_batch_id=False,
            )
            delivery_path = prepared.delivery_path
            payload = json.loads(delivery_path.read_text(encoding="utf-8"))
            payload.pop("output_batch_id", None)
            payload.pop("input_batch_id", None)
            payload.pop("client_instance_id", None)
            delivery_path.write_text(
                json.dumps(payload, ensure_ascii=False),
                encoding="utf-8",
            )

            output_store, artifacts = self._reopen(temporary)
            legacy = await artifacts.delivery_store.get(prepared.delivery_id)
            self.assertIsNone(legacy.output_batch_id)
            batch = await output_store.get(prepared.output_batch_id)
            self.assertEqual(batch.schema_version, 1)
            self.assertIsNone(batch.input_batch_id)

            report = await reconcile_unclaimable_legacy_ready(
                output_store,
                artifacts.delivery_store,
            )

            self.assertEqual(
                report.repaired_output_batch_ids,
                (prepared.output_batch_id,),
            )
            repaired = await artifacts.delivery_store.get(
                prepared.delivery_id
            )
            self.assertEqual(repaired.output_batch_id, prepared.output_batch_id)
            self.assertIsNone(repaired.input_batch_id)
            self.assertEqual(repaired.client_instance_id, "web-1")

    async def _write_unbound_ready(
        self,
        temporary: str,
        *,
        cycle_id: str,
        include_input_batch_id: bool,
    ):
        config = StorageConfigType(root_dir=temporary)
        storage = create_storage_services(config)
        artifacts = create_artifact_services(
            storage_config=config,
            artifact_config=ArtifactConfigType(),
            content_store=storage.content_store,
        )
        session_id = "web:conversation:ownership-recovery"
        artifact = await artifacts.artifact_service.create_text(
            session_id=session_id,
            cycle_id=cycle_id,
            filename="recovered.md",
            text="recovered",
            format_id="markdown",
            purpose=ArtifactPurpose.DELIVERABLE,
            provenance=ArtifactProvenance(
                origin="agent_created",
                creator="agent",
                operation="startup_recovery_test",
            ),
        )
        selected = await artifacts.delivery_service.select(
            artifact_id=artifact.artifact_id,
            access=ArtifactAccessContext(
                session_id=session_id,
                cycle_id=cycle_id,
                allowed_artifact_ids=[artifact.artifact_id],
            ),
            client_type="web",
        )
        snapshot = build_default_capability_registry().resolve(
            build_web_capability_declaration(),
            client_type="web",
            client_instance_id="web-1",
        )
        input_batch_id = new_input_batch_id()
        values = {
            "session_id": session_id,
            "cycle_id": cycle_id,
            "sequence_number": 1,
            "kind": OutputBatchKind.FINAL,
            "response_route": ClientResponseRoute(
                route_type="web",
                conversation_id="ownership-recovery",
            ),
            "locale": "ru",
            "capability_snapshot": snapshot,
            "parts": (
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
        }
        if include_input_batch_id:
            values["input_batch_id"] = input_batch_id
        output_store = FileSystemOutputBatchStore(Path(temporary))
        batch = build_ready_output_batch(**values)
        await output_store.commit(batch)
        return SimpleNamespace(
            output_batch_id=batch.output_batch_id,
            input_batch_id=input_batch_id,
            delivery_id=selected.delivery_id,
            delivery_path=(
                artifacts.delivery_store.root / f"{selected.delivery_id}.json"
            ),
        )

    @staticmethod
    def _reopen(temporary: str):
        config = StorageConfigType(root_dir=temporary)
        storage = create_storage_services(config)
        artifacts = create_artifact_services(
            storage_config=config,
            artifact_config=ArtifactConfigType(),
            content_store=storage.content_store,
        )
        return FileSystemOutputBatchStore(Path(temporary)), artifacts


if __name__ == "__main__":
    unittest.main()
