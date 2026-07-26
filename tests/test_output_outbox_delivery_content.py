import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from src.api.output_outbox_routes import create_output_outbox_router
from src.artifacts import new_artifact_delivery_id, new_artifact_id
from src.ingress.models import ClientResponseRoute
from src.interaction.capabilities import (
    build_default_capability_registry,
    build_telegram_capability_declaration,
)
from src.interaction.ids import new_output_part_id
from src.interaction.output_models import (
    ArtifactOutputPart,
    OutputBatchKind,
)
from src.interaction.output_store import (
    FileSystemOutputBatchStore,
    build_ready_output_batch,
)


class _ContentFacade:
    def __init__(self, store, *, part, payload: bytes):
        self.api = SimpleNamespace(output_store=store)
        self.part = part
        self.payload = payload
        self.claim_calls = 0
        self.open_calls = 0

    async def claim_delivery(self, delivery_id, *, session_id, client_type):
        self.claim_calls += 1
        return SimpleNamespace(
            delivery_id=delivery_id,
            artifact_id=self.part.artifact_id,
            filename=self.part.filename,
            mime_type=self.part.mime_type,
            size_bytes=self.part.size_bytes,
            content_hash="sha256:test",
        )

    async def open_delivery(self, delivery_id, *, session_id, client_type):
        self.open_calls += 1

        async def iterator():
            yield self.payload

        return iterator()


class OutputOutboxDeliveryContentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = FileSystemOutputBatchStore(Path(self.temporary.name))
        snapshot = build_default_capability_registry().resolve(
            build_telegram_capability_declaration(),
            client_type="telegram",
            client_instance_id="bot-1",
        )
        self.payload = b"exact-output"
        self.part = ArtifactOutputPart(
            part_id=new_output_part_id(),
            index=0,
            artifact_id=new_artifact_id(),
            delivery_id=new_artifact_delivery_id(),
            filename="result.bin",
            mime_type="application/octet-stream",
            size_bytes=len(self.payload),
        )
        batch = build_ready_output_batch(
            session_id="session-1",
            cycle_id="cycle-1",
            sequence_number=1,
            kind=OutputBatchKind.FINAL,
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="100",
            ),
            locale="en",
            capability_snapshot=snapshot,
            parts=(self.part,),
        )
        asyncio.run(self.store.commit(batch))
        asyncio.run(self.store.claim_delivery(batch.output_batch_id))
        self.output_batch_id = batch.output_batch_id

        self.facade = _ContentFacade(
            self.store,
            part=self.part,
            payload=self.payload,
        )

        async def auth(x_api_key: str = Header(alias="X-API-Key")) -> str:
            if x_api_key != "telegram-key":
                raise HTTPException(status_code=403, detail="Invalid API Key")
            return x_api_key

        app = FastAPI()
        app.include_router(
            create_output_outbox_router(
                facade=self.facade,
                auth_dependency=auth,
                api_key_scopes={
                    "telegram-key": frozenset({"telegram"}),
                },
            )
        )
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        self.client.close()
        self.temporary.cleanup()

    def test_exact_claimed_instance_can_stream_member_bytes(self):
        response = self._get(
            delivery_id=self.part.delivery_id,
            client_instance_id="bot-1",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.payload)
        self.assertEqual(
            response.headers["x-output-batch-id"],
            self.output_batch_id,
        )
        self.assertEqual(self.facade.claim_calls, 1)
        self.assertEqual(self.facade.open_calls, 1)

    def test_other_client_instance_cannot_open_delivery(self):
        response = self._get(
            delivery_id=self.part.delivery_id,
            client_instance_id="bot-2",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.facade.claim_calls, 0)
        self.assertEqual(self.facade.open_calls, 0)

    def test_delivery_outside_manifest_is_rejected_before_artifact_claim(self):
        response = self._get(
            delivery_id=new_artifact_delivery_id(),
            client_instance_id="bot-1",
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.facade.claim_calls, 0)
        self.assertEqual(self.facade.open_calls, 0)

    def _get(self, *, delivery_id: str, client_instance_id: str):
        return self.client.get(
            f"/internal/output-outbox/{self.output_batch_id}/deliveries/"
            f"{delivery_id}/content",
            params={
                "session_id": "session-1",
                "client_type": "telegram",
                "client_instance_id": client_instance_id,
            },
            headers={"X-API-Key": "telegram-key"},
        )


if __name__ == "__main__":
    unittest.main()
