import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
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


class OutputOutboxApiAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = FileSystemOutputBatchStore(root)
        registry = build_default_capability_registry()
        declaration = build_telegram_capability_declaration()
        self.snapshot = registry.resolve(
            declaration,
            client_type="telegram",
            client_instance_id="bot-1",
        )
        self.other_snapshot = registry.resolve(
            declaration,
            client_type="telegram",
            client_instance_id="bot-2",
        )
        batch = self._batch(
            cycle_id="cycle-1",
            kind=OutputBatchKind.FINAL,
        )
        asyncio.run(self.store.commit(batch))
        self.final_batch_id = batch.output_batch_id

        other = self._batch(
            cycle_id="cycle-other-instance",
            kind=OutputBatchKind.FINAL,
            snapshot=self.other_snapshot,
        )
        asyncio.run(self.store.commit(other))
        self.other_batch_id = other.output_batch_id

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

        renderer = SimpleNamespace(
            plan=lambda batch: SimpleNamespace(
                model_dump=lambda **_: {
                    "output_batch_id": batch.output_batch_id,
                    "operations": [],
                }
            )
        )
        facade = SimpleNamespace(api=SimpleNamespace(
            output_store=self.store,
            output_renderer=renderer,
        ))
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
                api_key_instance_scopes={
                    "telegram-key": frozenset({("telegram", "bot-1")}),
                    "web-key": frozenset({("web", "*")}),
                    "internal-key": frozenset({("*", "*")}),
                },
            )
        )
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        self.client.close()
        self.temporary.cleanup()

    def test_transport_key_cannot_read_another_transport_outbox(self):
        response = self._list(
            key="web-key",
            client_instance_id="bot-1",
        )
        self.assertEqual(response.status_code, 403)

    def test_matching_transport_key_can_read_exact_instance(self):
        response = self._list(
            key="telegram-key",
            client_instance_id="bot-1",
        )
        self.assertEqual(response.status_code, 200)
        batches = response.json()["output_batches"]
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0]["output_batch_id"], self.final_batch_id)

    def test_transport_key_cannot_select_another_instance(self):
        response = self._list(
            key="telegram-key",
            client_instance_id="bot-2",
        )
        self.assertEqual(response.status_code, 403)

        claim = self.client.post(
            f"/internal/output-outbox/{self.other_batch_id}/claim",
            json={
                "session_id": "session-1",
                "client_type": "telegram",
                "client_instance_id": "bot-2",
                "claim_request_id": new_output_claim_request_id(),
            },
            headers={"X-API-Key": "telegram-key"},
        )
        self.assertEqual(claim.status_code, 403)
        batch = asyncio.run(self.store.get(self.other_batch_id))
        self.assertEqual(batch.state, OutputBatchState.READY)

    def test_internal_key_can_administer_transport_outbox(self):
        response = self._list(
            key="internal-key",
            client_instance_id="bot-2",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["output_batches"]), 1)
        self.assertEqual(
            response.json()["output_batches"][0]["output_batch_id"],
            self.other_batch_id,
        )

    def test_exact_instance_can_read_ready_and_terminal_batch_by_id(self):
        ready = self._get(self.final_batch_id)
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.json()["output_batch"]["state"], "ready")

        batch, attempt_id = asyncio.run(
            self.store.claim_delivery(self.final_batch_id)
        )
        now = datetime.now(timezone.utc)
        receipt = OutputDeliveryReceipt(
            output_batch_id=batch.output_batch_id,
            attempt_id=attempt_id,
            state=OutputDeliveryReceiptState.DELIVERED,
            part_receipts=tuple(
                OutputPartReceipt(
                    part_id=part.part_id,
                    index=part.index,
                    required=part.required,
                    state=OutputPartReceiptState.DELIVERED,
                    client_message_ids=("web-response-1",),
                    delivered_at=now,
                )
                for part in batch.parts
            ),
            started_at=now,
            completed_at=now,
        )
        asyncio.run(self.store.complete(receipt))

        terminal = self._get(self.final_batch_id)
        self.assertEqual(terminal.status_code, 200)
        self.assertEqual(terminal.json()["output_batch"]["state"], "delivered")

    def test_get_by_id_enforces_session_and_instance_authority(self):
        wrong_session = self._get(self.final_batch_id, session_id="session-2")
        self.assertEqual(wrong_session.status_code, 403)
        wrong_instance = self._get(
            self.final_batch_id,
            client_instance_id="bot-2",
        )
        self.assertEqual(wrong_instance.status_code, 403)
        unknown = self._get("obat_" + "f" * 32)
        self.assertEqual(unknown.status_code, 404)

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

    def _list(self, *, key: str, client_instance_id: str):
        return self.client.get(
            "/internal/output-outbox/ready",
            params={
                "client_type": "telegram",
                "client_instance_id": client_instance_id,
                "minimum_age_seconds": 0,
            },
            headers={"X-API-Key": key},
        )

    def _get(
        self,
        output_batch_id: str,
        *,
        session_id: str = "session-1",
        client_instance_id: str = "bot-1",
    ):
        return self.client.get(
            f"/internal/output-outbox/{output_batch_id}",
            params={
                "session_id": session_id,
                "client_type": "telegram",
                "client_instance_id": client_instance_id,
            },
            headers={"X-API-Key": "telegram-key"},
        )

    def _batch(
        self,
        *,
        cycle_id: str,
        kind: OutputBatchKind,
        snapshot=None,
    ):
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
            capability_snapshot=snapshot or self.snapshot,
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
