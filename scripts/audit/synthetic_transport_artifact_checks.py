"""Additional synthetic checks used only by the v0.4 transport roast.

The regular suite already exercises most durable services.  These checks fill
two audit-specific holes: a real multipart ASGI ingress path and value-based
trace redaction probes.  Every test creates a new temporary storage root.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Header, HTTPException

from src.api.artifact_routes import create_artifact_router
from src.api.artifact_transport import ArtifactTransportFacade
from src.artifacts import (
    ArtifactConfigType,
    ArtifactTraceService,
    FileSystemArtifactTraceStore,
    create_artifact_services,
)
from src.ingress import IngressConfigType, create_ingress_services
from src.storage import StorageConfigType, create_storage_services


def _manifest(
    *,
    suffix: str,
    text: str = "synthetic instruction",
    slots: list[dict] | None = None,
) -> dict:
    slot_values = list(slots or [])
    return {
        "idempotency_key": f"audit-web-{suffix}",
        "client_type": "web",
        "client_instance_id": "audit-web-1",
        "conversation": {"conversation_id": f"conversation-{suffix}"},
        "sender": {"principal_id": "synthetic-principal"},
        "source_update_id": f"update-{suffix}",
        "source_message_id": f"message-{suffix}",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "text_parts": [
            {
                "part_id": f"text-{suffix}",
                "kind": "message_text",
                "text": text,
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
        "metadata": {"synthetic_audit": True},
    }


def _slot(
    slot_id: str,
    field_name: str | None,
    *,
    filename: str = "sample.bin",
    size: int = 4,
) -> dict:
    value = {
        "slot_id": slot_id,
        "media_kind": "document",
        "original_filename": filename,
        "declared_mime_type": "application/octet-stream",
        "declared_size_bytes": size,
    }
    if field_name is not None:
        value["upload_field_name"] = field_name
    return value


@pytest_asyncio.fixture
async def web_fixture(tmp_path: Path):
    storage_config = StorageConfigType(root_dir=str(tmp_path / "storage"))
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
    ingress = create_ingress_services(
        storage_config=storage_config,
        ingress_config=IngressConfigType(max_batch_total_bytes=2 * 1024 * 1024),
        content_store=storage.content_store,
        artifact_services=artifacts,
    )

    class ForbiddenMessageProcessor:
        async def process_committed_batch(self, *_args, **_kwargs):
            raise AssertionError("AgentCycle run is forbidden in synthetic audit")

    facade = ArtifactTransportFacade(
        api=SimpleNamespace(
            ingress_services=ingress,
            artifact_config=artifact_config,
        ),
        message_processor=ForbiddenMessageProcessor(),
    )

    async def auth(x_api_key: str | None = Header(default=None)) -> str:
        if x_api_key != "synthetic-web-key":
            raise HTTPException(status_code=403, detail="Invalid API Key")
        return x_api_key

    app = FastAPI()
    app.include_router(create_artifact_router(facade=facade, auth_dependency=auth))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://synthetic-asgi",
    ) as client:
        yield SimpleNamespace(
            client=client,
            ingress=ingress,
            artifacts=artifacts,
            storage_root=Path(storage_config.root_dir).resolve(),
        )


@pytest.mark.asyncio
async def test_web_real_router_valid_replay_and_binary_integrity(web_fixture):
    payload = b"\x00\x01synthetic\x00payload"
    slots = [
        _slot(
            "slot-1",
            "upload-1",
            filename="../Unicode-отчёт.bin",
            size=len(payload),
        )
    ]
    manifest = _manifest(suffix="valid", slots=slots)
    files = [
        ("manifest", (None, json.dumps(manifest), "application/json")),
        ("upload-1", ("ignored-client-path.bin", payload, "application/octet-stream")),
    ]
    headers = {"X-API-Key": "synthetic-web-key"}

    first = await web_fixture.client.post(
        "/web/input-batches?run=false",
        headers=headers,
        files=files,
    )
    replay = await web_fixture.client.post(
        "/web/input-batches",
        headers=headers,
        files=files,
    )

    assert first.status_code == 201, first.text
    assert replay.status_code == 200, replay.text
    assert replay.json()["duplicate"] is True
    assert replay.json()["input_batch_id"] == first.json()["input_batch_id"]
    batch = await web_fixture.ingress.batch_store.get_committed(
        first.json()["input_batch_id"]
    )
    assert len(batch.artifact_refs) == 1
    artifact = await web_fixture.artifacts.artifact_store.get_version(
        batch.artifact_refs[0]
    )
    assert artifact.size_bytes == len(payload)
    assert artifact.content_hash == "sha256:" + hashlib.sha256(payload).hexdigest()
    assert ".." not in artifact.filename
    assert "/" not in artifact.filename and "\\" not in artifact.filename
    assert web_fixture.storage_root.is_dir()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "files"),
    [
        ("missing-manifest", [("upload", ("a.bin", b"a", "application/octet-stream"))]),
        ("manifest-as-file", [("manifest", ("manifest.json", b"{}", "application/json"))]),
        ("invalid-json", [("manifest", (None, "{", "application/json"))]),
    ],
)
async def test_web_real_router_rejects_malformed_manifest(web_fixture, case, files):
    response = await web_fixture.client.post(
        "/web/input-batches",
        headers={"X-API-Key": "synthetic-web-key"},
        files=files,
    )
    assert response.status_code == 422, (case, response.status_code, response.text)


@pytest.mark.asyncio
async def test_web_real_router_rejects_missing_unexpected_and_duplicate_uploads(
    web_fixture,
):
    manifest = _manifest(
        suffix="fields",
        slots=[_slot("slot-fields", "expected", size=1)],
    )
    manifest_part = ("manifest", (None, json.dumps(manifest), "application/json"))
    headers = {"X-API-Key": "synthetic-web-key"}

    missing = await web_fixture.client.post(
        "/web/input-batches", headers=headers, files=[manifest_part]
    )
    unexpected = await web_fixture.client.post(
        "/web/input-batches",
        headers=headers,
        files=[manifest_part, ("other", ("a.bin", b"a", "application/octet-stream"))],
    )
    duplicate = await web_fixture.client.post(
        "/web/input-batches",
        headers=headers,
        files=[
            manifest_part,
            ("expected", ("a.bin", b"a", "application/octet-stream")),
            ("expected", ("b.bin", b"b", "application/octet-stream")),
        ],
    )

    assert missing.status_code == 422, missing.text
    assert unexpected.status_code == 422, unexpected.text
    assert duplicate.status_code == 422, duplicate.text


@pytest.mark.asyncio
async def test_web_real_router_rejects_two_slots_for_one_upload_field(web_fixture):
    manifest = _manifest(
        suffix="slot-collision",
        slots=[
            _slot("slot-a", "shared", size=1),
            _slot("slot-b", "shared", size=1),
        ],
    )
    response = await web_fixture.client.post(
        "/web/input-batches",
        headers={"X-API-Key": "synthetic-web-key"},
        files=[
            ("manifest", (None, json.dumps(manifest), "application/json")),
            ("shared", ("a.bin", b"a", "application/octet-stream")),
        ],
    )
    durable_files = sorted(
        str(path.relative_to(web_fixture.storage_root))
        for path in web_fixture.storage_root.rglob("*")
        if path.is_file()
    )
    assert response.status_code == 422, {
        "http_status": response.status_code,
        "body": response.text,
        "durable_files": durable_files,
    }


@pytest.mark.asyncio
async def test_web_real_router_rejects_extra_plain_form_field(web_fixture):
    manifest = _manifest(suffix="plain-field")
    response = await web_fixture.client.post(
        "/web/input-batches",
        headers={"X-API-Key": "synthetic-web-key"},
        files=[("manifest", (None, json.dumps(manifest), "application/json"))],
        data={"unexpected": "must-not-be-ignored"},
    )
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_web_real_router_rejects_wrong_transport_key(web_fixture):
    manifest = _manifest(suffix="wrong-key")
    response = await web_fixture.client.post(
        "/web/input-batches",
        headers={"X-API-Key": "telegram-shaped-but-invalid"},
        files=[("manifest", (None, json.dumps(manifest), "application/json"))],
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_trace_redacts_sensitive_values_not_only_sensitive_keys(tmp_path: Path):
    config = StorageConfigType(root_dir=str(tmp_path / "storage"))
    store = FileSystemArtifactTraceStore(config)
    service = ArtifactTraceService(store, max_string_chars=1024)
    probes = [
        "Bearer synthetic-secret",
        "api-key=synthetic-key",
        r"C:\\synthetic\\private\\file.txt",
        "/synthetic/private/file.txt",
        "https://example.invalid/private",
        "https://example.invalid/download?token=synthetic",
        "token=synthetic-token",
        "secret=synthetic-secret",
    ]
    await service.record(
        session_id="web:conversation:redaction-audit",
        event_type="artifact_ingress_stored",
        stage="ingress",
        status="succeeded",
        data={f"probe_{index}": value for index, value in enumerate(probes)},
    )
    session_dir = store._session_dir("web:conversation:redaction-audit")
    raw = "\n".join(
        path.read_text(encoding="utf-8") for path in session_dir.glob("*.jsonl")
    )
    for probe in probes:
        assert probe not in raw
