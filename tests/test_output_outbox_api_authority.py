import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from src.api.output_outbox_routes import create_output_outbox_router
from src.ingress.models import ClientResponseRoute
from src.interaction.capabilities import (
    build_default_capability_registry,
    build_telegram_capability_declaration,
)
from src.interaction.ids import (
    new_output_claim_request_id,
    new_output_part_id,
)
from src.interaction.output_models import (
    OutputBatchKind,
    OutputBatchState,
    TextOutputPart,
)
from src.interaction.output_store import (
    FileSystemOutputBatchStore,
    build_ready_output_batch,
)


class OutputOutboxApiAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = FileSystemOutputBatchStore(root)
        self.snapshot = build_default_capability_registry().resolve(
            build_telegram_capability_declaration(),
            client_type="telegram",
            client_instance_id="bot-1",
        )
        batch = self._batch(
            cycle_id="cycle-1",
            kind=OutputBatchKind.FINAL,
        )
        asyncio.run(self.store.commit(batch))
        self.final_batch_id = batch.output_batch_id

        intermediate = self._batch(
            cycle_id="cycle-intermediate",
            kind=OutputBatchKind.INTERMEDIATE,
        )
        asyncio.run(self.store.commit(intermediate))
        self.intermediate_batch_id = intermediate.output_batch_id

        async def auth(x_api_key: str = Header(alias="X-API-Key")) -> str:
            if x_api_key not in {"telegram-key", "web-key", "internal-key"}:
                raise HTTPException(status_code=403, detail="Invalid API Key")
            return x_api_key

        facade = SimpleNamespace(
            api=SimpleNamespace(output_store=self.store),
        )
        app = FastAPI()
        app.include_router(
            create_output_outbox_router(
                facade=facade,
                auth_dependency=auth,
                api_key_scopes={
                    "telegram-key": frozenset({"telegram"}),
                    "web-key": frozenset({"web"}),
                    "internal-key": frozenset({"*"}),
                },
            )
        )
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        self.client.close()
        self.temporary.cleanup()

    def test_transport_key_cannot_read_another_transport_outbox(self):
        response = self.client.get(
            "/internal/output-outbox/ready",
            params={
                "client_type": "telegram",
                "client_instance_id": "bot-1",
                "minimum_age_seconds": 0,
            },
            headers={"X-API-Key": "web-key"},
        )
        self.assertEqual(response.status_code, 403)

    def test_matching_transport_key_can_read_exact_instance(self):
        response = self.client.get(
            "/internal/output-outbox/ready",
            params={
                "client_type": "telegram",
                "client_instance_id": "bot-1",
                "minimum_age_seconds": 0,
            },
            headers={"X-API-Key": "telegram-key"},
        )
        self.assertEqual(response.status_code, 200)
        batches = response.json()["output_batches"]
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["output_batch_id"], self.final_batch_id)

    def test_internal_key_can_administer_transport_outbox(self):
        response = self.client.get(
            "/internal/output-outbox/ready",
            params={
                "client_type": "telegram",
                "client_instance_id": "bot-1",
                "minimum_age_seconds": 0,
            },
            headers={"X-API-Key": "internal-key"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["output_batches"]), 1)

    def test_direct_claim_rejects_non_final_batch_before_state_change(self):
        response = self.client.post(
            f"/internal/output-outbox/{self.intermediate_batch_id}/claim",
            json={
                "session_id": "session-1",
                "client_type": "telegram",
                "client_instance_id": "bot-1",
                "claim_request_id": new_output_claim_request_id(),
            },
            headers={"X-API-Key": "telegram-key"},
        )
        self.assertEqual(response.status_code, 409)
        batch = asyncio.run(self.store.get(self.intermediate_batch_id))
        self.assertEqual(batch.state, OutputBatchState.READY)

    def _batch(self, *, cycle_id: str, kind: OutputBatchKind):
        return build_ready_output_batch(
            session_id="session-1",
            cycle_id=cycle_id,
            sequence_number=1,
            kind=kind,
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="100",
            ),
            locale="en",
            capability_snapshot=self.snapshot,
            parts=(
                TextOutputPart(
                    part_id=new_output_part_id(),
                    index=0,
                    text="ready",
                ),
            ),
        )


if __name__ == "__main__":
    unittest.main()
