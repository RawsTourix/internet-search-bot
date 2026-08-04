import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from src.api.artifact_routes import create_artifact_router
from src.api.artifact_transport import ArtifactTransportFacade
from src.api.output_outbox_routes import create_output_outbox_router
from src.artifacts import (
    ArtifactAccessContext,
    ArtifactConfigType,
    ArtifactProvenance,
    ArtifactPurpose,
    create_artifact_services,
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
    build_web_capability_declaration,
)
from src.interaction.config import OutputRuntimeConfig
from src.interaction.output_completion import OutputDeliveryCompletionService
from src.interaction.output_service import OutputBatchAssembler
from src.interaction.output_store import FileSystemOutputBatchStore
from src.storage import create_storage_services
from src.storage.config import StorageConfigType


class WebOutputDeliveryHardeningTests(unittest.TestCase):
    def test_restart_then_http_delivery_receipt_recovery_and_legacy_guard(self):
        with tempfile.TemporaryDirectory() as temporary:
            prepared = asyncio.run(self._prepare(temporary))

            # Re-open every durable store before the Web worker sees the batch.
            config = StorageConfigType(root_dir=temporary)
            storage = create_storage_services(config)
            artifacts = create_artifact_services(
                storage_config=config,
                artifact_config=ArtifactConfigType(),
                content_store=storage.content_store,
            )
            output_store = FileSystemOutputBatchStore(Path(temporary))
            completion = OutputDeliveryCompletionService(
                output_store=output_store,
                artifact_delivery_store=artifacts.delivery_store,
            )
            renderer = OutputBatchAssembler(
                config=OutputRuntimeConfig(),
                delivery_store=artifacts.delivery_store,
                output_store=output_store,
            ).renderer
            api = SimpleNamespace(
                artifact_services=artifacts,
                output_store=output_store,
                output_completion=completion,
                output_renderer=renderer,
            )
            facade = ArtifactTransportFacade(
                api=api,
                message_processor=object(),
            )

            async def auth(x_api_key: str = Header(alias="X-API-Key")) -> str:
                if x_api_key != "web-test-key":
                    raise HTTPException(status_code=403, detail="Invalid API Key")
                return x_api_key

            app = FastAPI()
            app.include_router(
                create_artifact_router(
                    facade=facade,
                    auth_dependency=auth,
                )
            )
            app.include_router(
                create_output_outbox_router(
                    facade=facade,
                    auth_dependency=auth,
                    api_key_scopes={"web-test-key": frozenset({"web"})},
                    api_key_instance_scopes={
                        "web-test-key": frozenset({("web", "web-1")})
                    },
                )
            )

            authority = {
                "session_id": prepared.session_id,
                "client_type": "web",
                "client_instance_id": "web-1",
            }
            headers = {"X-API-Key": "web-test-key"}
            with TestClient(app, raise_server_exceptions=False) as client:
                ready_by_id = client.get(
                    f"/internal/output-outbox/{prepared.output_batch_id}",
                    params=authority,
                    headers=headers,
                )
                self.assertEqual(ready_by_id.status_code, 200)
                self.assertEqual(
                    ready_by_id.json()["output_batch"]["state"],
                    "ready",
                )

                legacy = client.post(
                    f"/internal/deliveries/{prepared.delivery_id}/complete",
                    json={
                        "session_id": prepared.session_id,
                        "client_type": "web",
                        "receipt": {"message_id": "legacy"},
                    },
                    headers=headers,
                )
                self.assertEqual(legacy.status_code, 409)
                self.assertIn("aggregate OutputBatch receipt", legacy.text)
                legacy_metadata = client.get(
                    f"/internal/deliveries/{prepared.delivery_id}",
                    params={
                        "session_id": prepared.session_id,
                        "client_type": "web",
                    },
                    headers=headers,
                )
                self.assertEqual(legacy_metadata.status_code, 409)

                other_session = client.get(
                    f"/internal/output-outbox/{prepared.output_batch_id}",
                    params={**authority, "session_id": "web:conversation:other"},
                    headers=headers,
                )
                self.assertEqual(other_session.status_code, 403)
                other_instance = client.get(
                    f"/internal/output-outbox/{prepared.output_batch_id}",
                    params={**authority, "client_instance_id": "web-2"},
                    headers=headers,
                )
                self.assertEqual(other_instance.status_code, 403)
                unknown = client.get(
                    "/internal/output-outbox/obat_ffffffffffffffffffffffffffffffff",
                    params=authority,
                    headers=headers,
                )
                self.assertEqual(unknown.status_code, 404)

                claimed = client.post(
                    f"/internal/output-outbox/{prepared.output_batch_id}/claim",
                    json={**authority, "claim_request_id": "oclm_" + "a" * 32},
                    headers=headers,
                )
                self.assertEqual(claimed.status_code, 200, claimed.text)
                claim_json = claimed.json()
                attempt_id = claim_json["attempt_id"]
                part = claim_json["output_batch"]["parts"][0]

                content = client.get(
                    f"/internal/output-outbox/{prepared.output_batch_id}/deliveries/"
                    f"{prepared.delivery_id}/content",
                    params=authority,
                    headers=headers,
                )
                self.assertEqual(content.status_code, 200, content.text)
                self.assertEqual(content.content, b"Web delivery works")
                self.assertEqual(content.headers["content-length"], "18")
                self.assertEqual(content.headers["content-type"], "text/markdown; charset=utf-8")
                self.assertIn("test-web-delivery.md", content.headers["content-disposition"])

                now = datetime.now(timezone.utc).isoformat()
                receipt = {
                    "output_batch_id": prepared.output_batch_id,
                    "attempt_id": attempt_id,
                    "state": "delivered",
                    "part_receipts": [{
                        "part_id": part["part_id"],
                        "index": part["index"],
                        "required": part["required"],
                        "state": "delivered",
                        "delivery_id": prepared.delivery_id,
                        "artifact_content_state": "delivered",
                        "client_message_ids": ["web-file-1"],
                        "delivered_at": now,
                    }],
                    "started_at": now,
                    "completed_at": now,
                }
                receipt_body = {**authority, "receipt": receipt}
                completed = client.post(
                    f"/internal/output-outbox/{prepared.output_batch_id}/receipt",
                    json=receipt_body,
                    headers=headers,
                )
                self.assertEqual(completed.status_code, 200, completed.text)
                self.assertEqual(completed.json()["state"], "delivered")
                replay = client.post(
                    f"/internal/output-outbox/{prepared.output_batch_id}/receipt",
                    json=receipt_body,
                    headers=headers,
                )
                self.assertEqual(replay.status_code, 200)
                self.assertEqual(replay.json(), completed.json())

                recovered = client.get(
                    f"/internal/output-outbox/{prepared.output_batch_id}",
                    params=authority,
                    headers=headers,
                )
                self.assertEqual(recovered.status_code, 200)
                self.assertEqual(
                    recovered.json()["output_batch"]["state"],
                    "delivered",
                )
                serialized = recovered.text
                self.assertNotIn(temporary, serialized)
                self.assertNotIn("web-test-key", serialized)

            record = asyncio.run(artifacts.delivery_store.get(prepared.delivery_id))
            self.assertEqual(record.state.value, "delivered")
            self.assertEqual(record.attempt_count, 1)
            traces = asyncio.run(artifacts.trace_store.list_session(prepared.session_id))
            terminal = [
                event
                for event in traces
                if event.event_type == "artifact_delivery_succeeded"
            ]
            self.assertEqual(len(terminal), 1)
            self.assertEqual(
                terminal[0].correlation.output_batch_id,
                prepared.output_batch_id,
            )

    def test_ready_batch_with_terminal_artifact_is_rejected_before_claim(self):
        with tempfile.TemporaryDirectory() as temporary:
            prepared = asyncio.run(self._prepare(temporary))
            config = StorageConfigType(root_dir=temporary)
            storage = create_storage_services(config)
            artifacts = create_artifact_services(
                storage_config=config,
                artifact_config=ArtifactConfigType(),
                content_store=storage.content_store,
            )
            asyncio.run(artifacts.delivery_service.claim(prepared.delivery_id))
            asyncio.run(artifacts.delivery_service.complete(
                prepared.delivery_id,
                receipt={"message_ids": ["corrupt-terminal"]},
            ))
            output_store = FileSystemOutputBatchStore(Path(temporary))
            completion = OutputDeliveryCompletionService(
                output_store=output_store,
                artifact_delivery_store=artifacts.delivery_store,
            )
            renderer = OutputBatchAssembler(
                config=OutputRuntimeConfig(),
                delivery_store=artifacts.delivery_store,
                output_store=output_store,
            ).renderer
            facade = ArtifactTransportFacade(
                api=SimpleNamespace(
                    artifact_services=artifacts,
                    output_store=output_store,
                    output_completion=completion,
                    output_renderer=renderer,
                ),
                message_processor=object(),
            )

            async def auth(x_api_key: str = Header(alias="X-API-Key")) -> str:
                if x_api_key != "web-test-key":
                    raise HTTPException(status_code=403, detail="Invalid API Key")
                return x_api_key

            app = FastAPI()
            app.include_router(create_output_outbox_router(
                facade=facade,
                auth_dependency=auth,
                api_key_scopes={"web-test-key": frozenset({"web"})},
                api_key_instance_scopes={
                    "web-test-key": frozenset({("web", "web-1")})
                },
            ))
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post(
                    f"/internal/output-outbox/{prepared.output_batch_id}/claim",
                    json={
                        "session_id": prepared.session_id,
                        "client_type": "web",
                        "client_instance_id": "web-1",
                        "claim_request_id": "oclm_" + "c" * 32,
                    },
                    headers={"X-API-Key": "web-test-key"},
                )
            self.assertEqual(response.status_code, 409)
            self.assertEqual(
                asyncio.run(output_store.get(prepared.output_batch_id)).state.value,
                "ready",
            )

    async def _prepare(self, temporary: str):
        config = StorageConfigType(root_dir=temporary)
        storage = create_storage_services(config)
        artifacts = create_artifact_services(
            storage_config=config,
            artifact_config=ArtifactConfigType(),
            content_store=storage.content_store,
        )
        session_id = "web:conversation:hardening"
        cycle_id = "cycle-web-hardening"
        artifact = await artifacts.artifact_service.create_text(
            session_id=session_id,
            cycle_id=cycle_id,
            filename="test-web-delivery.md",
            text="Web delivery works",
            format_id="markdown",
            purpose=ArtifactPurpose.DELIVERABLE,
            provenance=ArtifactProvenance(
                origin="agent_created",
                creator="agent",
                operation="web_hardening_test",
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
        now = datetime.now(timezone.utc)
        input_batch = CommittedInputBatch(
            input_batch_id=new_input_batch_id(),
            session_id=session_id,
            client_type=ClientType.WEB,
            sequence_number=1,
            source_event_ids=[new_ingress_event_id()],
            admission_mode="new_cycle",
            response_route=ClientResponseRoute(
                route_type="web",
                conversation_id="hardening",
            ),
            locale="ru",
            capability_snapshot=snapshot,
            committed_at=now,
            commit_reason="web_hardening_test",
            content_fingerprint="sha256:" + "b" * 64,
        )
        output_store = FileSystemOutputBatchStore(Path(temporary))
        assembler = OutputBatchAssembler(
            config=OutputRuntimeConfig(),
            delivery_store=artifacts.delivery_store,
            output_store=output_store,
        )
        batch = await assembler.assemble_final(
            result=AgentResult(
                content="",
                status=AgentStatus.DONE,
                session_id=session_id,
                cycle_id=cycle_id,
            ),
            input_batch=input_batch,
        )
        return SimpleNamespace(
            session_id=session_id,
            input_batch_id=input_batch.input_batch_id,
            artifact_id=artifact.artifact_id,
            delivery_id=selected.delivery_id,
            output_batch_id=batch.output_batch_id,
        )


if __name__ == "__main__":
    unittest.main()
