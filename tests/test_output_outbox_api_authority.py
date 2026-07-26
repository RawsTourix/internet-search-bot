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
from src.interaction.ids import new_output_part_id
from src.interaction.output_models import OutputBatchKind, TextOutputPart
from src.interaction.output_store import (
    FileSystemOutputBatchStore,
    build_ready_output_batch,
)


class OutputOutboxApiAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        store = FileSystemOutputBatchStore(root)
        snapshot = build_default_capability_registry().resolve(
            build_telegram_capability_declaration(),
            client_type="telegram",
            client_instance_id="bot-1",
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
            parts=(
                TextOutputPart(
                    part_id=new_output_part_id(),
                    index=0,
                    text="ready",
                ),
            ),
        )
        asyncio.run(store.commit(batch))

        async def auth(x_api_key: str = Header(alias="X-API-Key")) -> str:
            if x_api_key not in {"telegram-key", "web-key", "internal-key"}:
                raise HTTPException(status_code=403, detail="Invalid API Key")
            return x_api_key

        facade = SimpleNamespace(
            api=SimpleNamespace(output_store=store),
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
        self.assertEqual(len(response.json()["output_batches"]), 1)

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


if __name__ == "__main__":
    unittest.main()
