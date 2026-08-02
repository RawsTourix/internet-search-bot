"""Regression tests for strict Web multipart ingress validation."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, Header, HTTPException

from src.api.artifact_routes import create_artifact_router
from src.api.artifact_transport import ArtifactTransportFacade
from src.artifacts import ArtifactConfigType, create_artifact_services
from src.ingress import IngressConfigType, create_ingress_services
from src.storage import StorageConfigType, create_storage_services


def _manifest(*, suffix: str, slots: list[dict] | None = None) -> dict:
    slot_values = list(slots or [])
    return {
        "idempotency_key": f"web-multipart-{suffix}",
        "client_type": "web",
        "client_instance_id": "web-regression",
        "conversation": {"conversation_id": f"conversation-{suffix}"},
        "sender": {"principal_id": "regression-user"},
        "source_update_id": f"update-{suffix}",
        "source_message_id": f"message-{suffix}",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "text_parts": [
            {
                "part_id": f"text-{suffix}",
                "kind": "message_text",
                "text": "synthetic regression input",
                "attachment_slot_ids": [item["slot_id"] for item in slot_values],
            }
        ],
        "attachment_slots": slot_values,
        "locale": "ru",
        "admission_mode": "new_cycle",
        "response_route": {
            "route_type": "web",
            "conversation_id": f"conversation-{suffix}",
        },
        "metadata": {"regression_test": True},
    }


def _slot(slot_id: str, field_name: str, *, size: int = 1) -> dict:
    return {
        "slot_id": slot_id,
        "media_kind": "document",
        "original_filename": f"{slot_id}.bin",
        "declared_mime_type": "application/octet-stream",
        "declared_size_bytes": size,
        "upload_field_name": field_name,
    }


class WebMultipartIngressValidationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.storage_root = Path(self.temporary.name) / "storage"
        storage_config = StorageConfigType(root_dir=str(self.storage_root))
        storage = create_storage_services(storage_config)
        artifact_config = ArtifactConfigType(
            max_artifact_size_bytes=1024 * 1024,
            max_patchable_text_bytes=1024 * 1024,
            max_workspace_bytes=2 * 1024 * 1024,
        )
        artifacts = create_artifact_services(
            storage_config=storage_config,
            artifact_config=artifact_config,
            content_store=storage.content_store,
        )
        self.ingress = create_ingress_services(
            storage_config=storage_config,
            ingress_config=IngressConfigType(
                max_batch_total_bytes=2 * 1024 * 1024
            ),
            content_store=storage.content_store,
            artifact_services=artifacts,
        )

        class ForbiddenMessageProcessor:
            async def process_committed_batch(self, *_args, **_kwargs):
                raise AssertionError(
                    "AgentCycle must not run in multipart tests"
                )

        facade = ArtifactTransportFacade(
            api=SimpleNamespace(
                ingress_services=self.ingress,
                artifact_config=artifact_config,
            ),
            message_processor=ForbiddenMessageProcessor(),
        )

        async def auth(x_api_key: str | None = Header(default=None)) -> str:
            if x_api_key != "web-regression-key":
                raise HTTPException(status_code=403, detail="Invalid API Key")
            return x_api_key

        app = FastAPI()
        app.include_router(
            create_artifact_router(
                facade=facade,
                auth_dependency=auth,
            )
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=app,
                raise_app_exceptions=False,
            ),
            base_url="http://web-regression",
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()
        self.temporary.cleanup()

    def _durable_files(self) -> list[str]:
        return sorted(
            str(path.relative_to(self.storage_root))
            for path in self.storage_root.rglob("*")
            if path.is_file()
        )

    async def test_duplicate_slot_upload_field_is_rejected_before_admission(
        self,
    ) -> None:
        manifest = _manifest(
            suffix="duplicate-slot-field",
            slots=[
                _slot("slot-a", "shared"),
                _slot("slot-b", "shared"),
            ],
        )

        response = await self.client.post(
            "/web/input-batches",
            headers={"X-API-Key": "web-regression-key"},
            files=[
                (
                    "manifest",
                    (None, json.dumps(manifest), "application/json"),
                ),
                (
                    "shared",
                    ("sample.bin", b"x", "application/octet-stream"),
                ),
            ],
        )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn(
            "multiple attachment slots",
            response.json()["detail"],
        )
        self.assertEqual(self._durable_files(), [])

    async def test_unexpected_plain_field_is_rejected_before_admission(
        self,
    ) -> None:
        manifest = _manifest(suffix="unexpected-plain")

        response = await self.client.post(
            "/web/input-batches",
            headers={"X-API-Key": "web-regression-key"},
            files=[
                (
                    "manifest",
                    (None, json.dumps(manifest), "application/json"),
                ),
            ],
            data={"unexpected": "must-not-be-ignored"},
        )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn(
            "Unexpected multipart form fields",
            response.json()["detail"],
        )
        self.assertEqual(self._durable_files(), [])

    async def test_duplicate_manifest_is_rejected_before_admission(
        self,
    ) -> None:
        first = _manifest(suffix="duplicate-manifest-a")
        second = _manifest(suffix="duplicate-manifest-b")

        response = await self.client.post(
            "/web/input-batches",
            headers={"X-API-Key": "web-regression-key"},
            files=[
                (
                    "manifest",
                    (None, json.dumps(first), "application/json"),
                ),
                (
                    "manifest",
                    (None, json.dumps(second), "application/json"),
                ),
            ],
        )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn(
            "exactly one JSON manifest",
            response.json()["detail"],
        )
        self.assertEqual(self._durable_files(), [])

    async def test_valid_unique_mapping_still_commits(self) -> None:
        manifest = _manifest(
            suffix="valid",
            slots=[_slot("slot-valid", "upload-valid", size=3)],
        )

        response = await self.client.post(
            "/web/input-batches?run=false",
            headers={"X-API-Key": "web-regression-key"},
            files=[
                (
                    "manifest",
                    (None, json.dumps(manifest), "application/json"),
                ),
                (
                    "upload-valid",
                    ("sample.bin", b"abc", "application/octet-stream"),
                ),
            ],
        )

        self.assertEqual(response.status_code, 201, response.text)
        payload = response.json()
        self.assertEqual(payload["status"], "committed")
        batch = await self.ingress.batch_store.get_committed(
            payload["input_batch_id"]
        )
        self.assertEqual(len(batch.artifact_refs), 1)


if __name__ == "__main__":
    unittest.main()
