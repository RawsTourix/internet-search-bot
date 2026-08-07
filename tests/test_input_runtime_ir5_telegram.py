from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from src.core.message_processor import MessageProcessor
from src.core.models import ClientType
from src.servers.telegram import runtime_control_handlers
from src.servers.telegram.runtime_state import (
    _install_ir5_runtime_control_handlers_if_host_ready,
)


class FakeApplication:
    def __init__(self):
        self.handlers: list[tuple[object, int]] = []

    def add_handler(self, handler, group=0):
        self.handlers.append((handler, group))


class FakeGenerationRegistry:
    def current(self, session_id: str) -> int:
        assert session_id == "telegram:conversation:42:thread:7"
        return 3


@pytest.mark.asyncio
async def test_telegram_stop_handler_uses_exact_session_and_stable_source_identity(monkeypatch):
    captured: dict = {}
    delivered: dict = {}

    async def status_message(update, text):
        captured["status_text"] = text
        return SimpleNamespace(message_id=900)

    def attach_progress_metadata(*, payload, update, status_message):
        payload["metadata"]["status_message_id"] = status_message.message_id

    async def send_to_gateway(payload):
        captured["payload"] = payload
        return True, "pause pending", {"progress_locale": "ru"}

    async def deliver(**kwargs):
        delivered.update(kwargs)

    fake_server = SimpleNamespace(
        TELEGRAM_BOT_INSTANCE_ID="bot-main",
        _session_for_update=lambda update: "telegram:conversation:42:thread:7",
        detect_progress_locale=lambda update: "ru",
        _localized=lambda key, locale: key,
        send_initial_status_message=status_message,
        attach_progress_metadata=attach_progress_metadata,
        send_to_gateway=send_to_gateway,
        session_generations=FakeGenerationRegistry(),
        _deliver_agent_result=deliver,
    )
    monkeypatch.setitem(
        sys.modules,
        "src.servers.telegram.telegram_server",
        fake_server,
    )
    import src.servers.telegram as telegram_package

    monkeypatch.setattr(telegram_package, "telegram_server", fake_server, raising=False)
    update = SimpleNamespace(
        update_id=1234,
        effective_chat=SimpleNamespace(id=42),
        effective_user=SimpleNamespace(id=5, full_name="User"),
        effective_message=SimpleNamespace(
            text="/stop",
            message_id=88,
            message_thread_id=7,
        ),
    )

    await runtime_control_handlers.runtime_control_handler(update, SimpleNamespace())

    payload = captured["payload"]
    assert payload["content"] == "/stop"
    assert payload["client_type"] == "telegram"
    assert payload["metadata"] == {
        "bot_instance_id": "bot-main",
        "chat_id": 42,
        "conversation_id": "42",
        "message_id": 88,
        "message_thread_id": 7,
        "thread_id": 7,
        "update_id": 1234,
        "session_id": "telegram:conversation:42:thread:7",
        "progress_locale": "ru",
        "status_message_id": 900,
    }
    assert delivered["session_id"] == "telegram:conversation:42:thread:7"
    assert delivered["metadata"]["telegram_session_generation"] == 3


def test_runtime_control_handlers_are_high_priority_and_ingress_cancel_is_untouched():
    application = FakeApplication()
    runtime_control_handlers.install_runtime_control_handlers(application)
    runtime_control_handlers.install_runtime_control_handlers(application)
    assert len(application.handlers) == 1
    handler, group = application.handlers[0]
    assert group == -10
    assert set(handler.commands) == {"stop", "continue"}
    assert "cancel" not in handler.commands
    assert "collect" not in handler.commands
    assert "send" not in handler.commands


def test_runtime_state_composition_seam_installs_into_real_host_shape(monkeypatch):
    application = FakeApplication()
    fake_host = SimpleNamespace(application=application)
    monkeypatch.setitem(
        sys.modules,
        "test.servers.telegram.telegram_server",
        fake_host,
    )
    _install_ir5_runtime_control_handlers_if_host_ready()
    assert len(application.handlers) == 1
    assert application.handlers[0][1] == -10


def test_canonical_telegram_app_registers_ir5_and_collection_handlers_once():
    """Exercise real production composition without invoking the compatibility installer."""
    from src.servers.telegram import app as canonical_app

    handlers = canonical_app.server.application.handlers
    runtime_controls = [
        handler
        for handler in handlers.get(-10, [])
        if set(getattr(handler, "commands", set())) == {"stop", "continue"}
    ]
    collection_controls = [
        handler
        for handler in handlers.get(-1, [])
        if set(getattr(handler, "commands", set())) == {"collect", "send", "cancel"}
    ]

    assert len(runtime_controls) == 1
    assert len(collection_controls) == 1
    assert "reset" not in runtime_controls[0].commands
    assert set(runtime_controls[0].commands).isdisjoint({"collect", "send", "cancel"})


def test_telegram_control_idempotency_key_is_stable_and_source_message_specific():
    processor = MessageProcessor()

    def message(message_id: int, *, thread_id: int = 7):
        return SimpleNamespace(
            client_type=ClientType.TELEGRAM,
            user_id="5",
            metadata={
                "bot_instance_id": "bot-main",
                "chat_id": 42,
                "message_thread_id": thread_id,
                "message_id": message_id,
                "update_id": 1000 + message_id,
            },
        )

    first = processor._control_idempotency_key(message(88), command="/stop")
    duplicate = processor._control_idempotency_key(message(88), command="/stop")
    different_message = processor._control_idempotency_key(message(89), command="/stop")
    different_thread = processor._control_idempotency_key(
        message(88, thread_id=8),
        command="/stop",
    )
    different_command = processor._control_idempotency_key(
        message(88),
        command="/continue",
    )

    assert duplicate == first
    assert different_message != first
    assert different_thread != first
    assert different_command != first
