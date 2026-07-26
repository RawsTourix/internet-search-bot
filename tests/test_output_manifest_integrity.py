import json
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from src.ingress.models import ClientResponseRoute
from src.interaction.capabilities import (
    build_default_capability_registry,
    build_telegram_capability_declaration,
    build_web_capability_declaration,
)
from src.interaction.errors import InteractionIntegrityError
from src.interaction.ids import new_output_part_id
from src.interaction.output_models import OutputBatchKind, TextOutputPart
from src.interaction.output_store import (
    FileSystemOutputBatchStore,
    build_ready_output_batch,
)


class OutputManifestIntegrityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
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

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_exact_manifest_tampering_is_rejected(self):
        store = FileSystemOutputBatchStore(self.root)
        batch, _ = await store.commit(self._batch())
        manifest_path = (
            store.records / batch.output_batch_id / "manifest.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["parts"][0]["part_id"] = new_output_part_id()
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        with self.assertRaises(InteractionIntegrityError):
            await store.get(batch.output_batch_id)

    async def test_semantic_manifest_tampering_is_rejected_for_legacy_index(self):
        store = FileSystemOutputBatchStore(self.root)
        batch, _ = await store.commit(self._batch())
        identity = store._identity(batch.session_id, batch.cycle_id, batch.kind)
        index_path = store.cycle_index / f"{identity}.json"
        pointer = json.loads(index_path.read_text(encoding="utf-8"))
        pointer.pop("manifest_hash")
        index_path.write_text(
            json.dumps(pointer, ensure_ascii=False),
            encoding="utf-8",
        )
        manifest_path = (
            store.records / batch.output_batch_id / "manifest.json"
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["parts"][0]["text"] = "tampered"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False),
            encoding="utf-8",
        )
        with self.assertRaises(InteractionIntegrityError):
            await store.get(batch.output_batch_id)

    def test_route_and_capability_client_types_must_match(self):
        with self.assertRaises(ValidationError):
            build_ready_output_batch(
                session_id="session-1",
                cycle_id="cycle-1",
                sequence_number=1,
                kind=OutputBatchKind.FINAL,
                response_route=ClientResponseRoute(
                    route_type="telegram",
                    conversation_id="chat-1",
                ),
                locale="ru",
                capability_snapshot=self.web_snapshot,
                parts=(
                    TextOutputPart(
                        part_id=new_output_part_id(),
                        index=0,
                        text="result",
                    ),
                ),
            )

    def _batch(self):
        return build_ready_output_batch(
            session_id="session-1",
            cycle_id="cycle-1",
            sequence_number=1,
            kind=OutputBatchKind.FINAL,
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="chat-1",
            ),
            locale="ru",
            capability_snapshot=self.telegram_snapshot,
            parts=(
                TextOutputPart(
                    part_id=new_output_part_id(),
                    index=0,
                    text="result",
                ),
            ),
        )


if __name__ == "__main__":
    unittest.main()
