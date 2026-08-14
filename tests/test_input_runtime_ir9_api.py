from __future__ import annotations

from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, Header
from fastapi.testclient import TestClient

from src.api.runtime_diagnostics_routes import add_runtime_diagnostics_routes
from src.input_runtime.diagnostics import (
    RuntimeControlStatus,
    RuntimeEmissionCounts,
    RuntimeInputStatus,
    RuntimeStatusCounts,
    RuntimeStatusSnapshot,
    RuntimeTimeline,
)


class FakeDiagnostics:
    DEFAULT_TIMELINE_LIMIT = 20
    MAX_TIMELINE_LIMIT = 100

    def __init__(self) -> None:
        self.status_sessions: list[str] = []
        self.timeline_sessions: list[tuple[str, int]] = []

    async def status(self, session_id: str) -> RuntimeStatusSnapshot:
        self.status_sessions.append(session_id)
        return RuntimeStatusSnapshot(
            process_readiness="ready",
            session_exists=False,
            generation=0,
            input=RuntimeInputStatus(
                accepted_sequence=0,
                applied_sequence=0,
            ),
            controls=RuntimeControlStatus(
                pending_sequence=0,
                applied_sequence=0,
            ),
            counts=RuntimeStatusCounts(),
            emissions=RuntimeEmissionCounts(),
        )

    async def timeline(self, session_id: str, *, limit: int) -> RuntimeTimeline:
        self.timeline_sessions.append((session_id, limit))
        return RuntimeTimeline(
            session_id=session_id,
            generation=0,
            limit=limit,
        )


def app_and_service():
    diagnostics = FakeDiagnostics()
    app = FastAPI()
    router = APIRouter()

    async def auth(x_api_key: str = Header(...)) -> str:
        return x_api_key

    add_runtime_diagnostics_routes(
        router,
        facade=SimpleNamespace(
            api=SimpleNamespace(input_runtime_diagnostics=diagnostics)
        ),
        auth_dependency=auth,
        api_key_scopes={
            "web-key": frozenset({"web"}),
            "telegram-key": frozenset({"telegram"}),
            "internal-key": frozenset({"*"}),
        },
    )
    app.include_router(router)
    return app, diagnostics


def test_ir9_web_status_is_fenced_into_web_session_namespace():
    app, diagnostics = app_and_service()
    response = TestClient(app).get(
        "/runtime/status",
        params={"session_id": "opaque-browser-session"},
        headers={"x-api-key": "web-key"},
    )
    assert response.status_code == 200
    assert response.json()["schema_version"] == "ir9.v1"
    assert diagnostics.status_sessions == ["web:session:opaque-browser-session"]


def test_ir9_web_key_cannot_inject_internal_or_telegram_session_id():
    app, diagnostics = app_and_service()
    response = TestClient(app).get(
        "/runtime/status",
        params={"session_id": "telegram:bot-a:10:root"},
        headers={"x-api-key": "web-key"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "runtime_diagnostics_session_forbidden"
    assert diagnostics.status_sessions == []


def test_ir9_telegram_scope_cannot_use_generic_http_diagnostics_route():
    app, diagnostics = app_and_service()
    response = TestClient(app).get(
        "/runtime/status",
        params={"session_id": "anything"},
        headers={"x-api-key": "telegram-key"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "runtime_diagnostics_scope_forbidden"
    assert diagnostics.status_sessions == []


def test_ir9_internal_scope_may_query_explicit_exact_session_and_bounded_timeline():
    app, diagnostics = app_and_service()
    session_id = "telegram:bot-a:10:root"
    response = TestClient(app).get(
        "/runtime/timeline",
        params={"session_id": session_id, "limit": 17},
        headers={"x-api-key": "internal-key"},
    )
    assert response.status_code == 200
    assert response.json()["session_id"] == session_id
    assert response.json()["limit"] == 17
    assert diagnostics.timeline_sessions == [(session_id, 17)]


def test_ir9_http_timeline_limit_is_bounded_before_repository_query():
    app, diagnostics = app_and_service()
    response = TestClient(app).get(
        "/runtime/timeline",
        params={"session_id": "opaque", "limit": 101},
        headers={"x-api-key": "web-key"},
    )
    assert response.status_code == 422
    assert diagnostics.timeline_sessions == []
