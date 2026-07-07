import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.agent.protocol import ProgressEvent
from src.core.models import ClientType, UnifiedMessage
from src.gateway import (
    is_allowed_progress_callback_url,
    make_http_progress_callback,
)
from src.core.errors import LLMHTTPError, LLMTimeoutError, LLMTransportError
from src.mcp.mcp_client import MCPClient, MCPToolBinding, SessionState
from src.servers.telegram import telegram_server


class ProgressRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = object.__new__(MCPClient)
        self.client.tool_registry = {}

    def test_progress_event_has_trace_fields(self):
        event = ProgressEvent(type="cycle_started", message="Starting")

        self.assertTrue(event.event_id)
        self.assertGreater(event.created_at, 0)
        self.assertEqual(event.visibility, "user")
        self.assertEqual(event.severity, "info")

    def test_progress_data_is_sanitized_and_truncated(self):
        result = self.client._safe_progress_data({
            "token": "secret-token",
            "nested": {"password": "secret-password"},
            "text": "x" * 600,
            "items": list(range(30)),
        })

        self.assertEqual(result["token"], "[REDACTED]")
        self.assertEqual(result["nested"]["password"], "[REDACTED]")
        self.assertTrue(result["text"].endswith("…[truncated]"))
        self.assertEqual(len(result["items"]), 21)

    def test_mcp_call_tool_separates_target_and_server(self):
        self.client.tool_registry["events"] = MCPToolBinding(
            public_name="events",
            server_name="kudago_nominatim",
            server_alias="",
            remote_name="events",
            description="",
            input_schema={},
        )

        manager, target = self.client._resolve_progress_tool_names(
            "mcp_call_tool",
            {"tool_name": "events", "arguments": {}},
        )

        self.assertEqual(manager, "mcp_call_tool")
        self.assertEqual(target, "events")
        self.assertEqual(
            self.client._resolve_progress_server_name(target),
            "kudago_nominatim",
        )

    async def test_callback_failure_does_not_break_runtime(self):
        state = SessionState()

        async def broken_callback(_event):
            raise RuntimeError("callback unavailable")

        await self.client._emit_progress(
            state,
            ProgressEvent(type="cycle_started", message="Starting"),
            broken_callback,
        )

        self.assertEqual(len(state.progress_events), 1)

    async def test_llm_http_429_emits_retry_then_succeeds(self):
        self.client.llm_max_retries = 1
        self.client.llm_retryable_http_statuses = {429, 500, 502, 503, 504}
        self.client._call_llm = AsyncMock(side_effect=[
            LLMHTTPError(429, "rate limited", retry_after=60.0),
            {"content": "ok"},
        ])
        self.client._get_llm_retry_delay = lambda _error, _attempt: 60.0
        state = SessionState(progress_locale="ru")
        events = []

        with patch("src.mcp.mcp_client.asyncio.sleep", new_callable=AsyncMock):
            result = await self.client._call_llm_with_retries(
                [],
                [],
                context="Итерация 4",
                state=state,
                session_id="session-1",
                cycle_id="cycle-1",
                progress_callback=events.append,
                cycle_trace=[],
            )

        self.assertEqual(result, {"content": "ok"})
        self.assertEqual(events[0]["type"], "llm_retry")
        self.assertIn("LLM HTTP 429", events[0]["message"])
        self.assertEqual(events[0]["data"]["retry_after"], 60.0)

    async def test_llm_transport_exhausted_emits_error(self):
        self.client.llm_max_retries = 0
        self.client._call_llm = AsyncMock(
            side_effect=LLMTransportError(
                "transport failed",
                cause_type="ConnectError",
                original_repr="ConnectError('')",
            )
        )
        state = SessionState(progress_locale="ru")
        events = []

        with self.assertRaises(LLMTransportError):
            await self.client._call_llm_with_retries(
                [],
                [],
                context="Итерация 16",
                state=state,
                session_id="session-1",
                cycle_id="cycle-1",
                progress_callback=events.append,
                cycle_trace=[],
            )

        self.assertEqual(events[0]["type"], "llm_error")
        self.assertIn("LLM transport error", events[0]["message"])
        self.assertEqual(events[0]["data"]["attempt"], 1)

    async def test_llm_http_429_exhausted_emits_error(self):
        self.client.llm_max_retries = 0
        self.client.llm_retryable_http_statuses = {429, 500, 502, 503, 504}
        self.client._call_llm = AsyncMock(
            side_effect=LLMHTTPError(429, "rate limited", retry_after=60.0)
        )
        state = SessionState(progress_locale="ru")
        events = []

        with self.assertRaises(LLMHTTPError):
            await self.client._call_llm_with_retries(
                [],
                [],
                context="Итерация 5",
                state=state,
                session_id="session-1",
                cycle_id="cycle-1",
                progress_callback=events.append,
                cycle_trace=[],
            )

        self.assertEqual(events[0]["type"], "llm_error")
        self.assertIn("LLM HTTP 429", events[0]["message"])
        self.assertIn("Повторы исчерпаны", events[0]["message"])

    async def test_llm_timeout_retry_emits_progress(self):
        self.client.llm_max_retries = 1
        self.client._call_llm = AsyncMock(side_effect=[
            LLMTimeoutError("request timed out"),
            {"content": "ok"},
        ])
        self.client._get_llm_retry_delay = lambda _error, _attempt: 10.0
        state = SessionState(progress_locale="en")
        events = []

        with patch("src.mcp.mcp_client.asyncio.sleep", new_callable=AsyncMock):
            await self.client._call_llm_with_retries(
                [],
                [],
                context="Iteration 2",
                state=state,
                session_id="session-1",
                cycle_id="cycle-1",
                progress_callback=events.append,
                cycle_trace=[],
            )

        self.assertEqual(events[0]["type"], "llm_retry")
        self.assertIn("LLM timeout", events[0]["message"])
        self.assertIn("Retrying in 10s", events[0]["message"])


class GatewayProgressTests(unittest.IsolatedAsyncioTestCase):
    def test_callback_allowlist_uses_origin_not_string_prefix(self):
        self.assertTrue(
            is_allowed_progress_callback_url(
                "http://127.0.0.1:8001/internal/progress"
            )
        )
        self.assertFalse(
            is_allowed_progress_callback_url(
                "http://127.0.0.1.evil.example/internal/progress"
            )
        )

    async def test_gateway_forwards_token_target_and_event(self):
        message = UnifiedMessage(
            id="request-1",
            timestamp="2026-07-07T12:00:00",
            client_type=ClientType.TELEGRAM,
            message_type="text",
            content="hello",
            user_id="1",
            metadata={
                "progress_callback_url": (
                    "http://127.0.0.1:8001/internal/progress"
                ),
                "progress_callback_token": "progress-secret",
                "progress_target": {"chat_id": 10, "message_id": 20},
            },
        )
        response = SimpleNamespace(raise_for_status=lambda: None)
        client = AsyncMock()
        client.post.return_value = response
        context_manager = AsyncMock()
        context_manager.__aenter__.return_value = client
        context_manager.__aexit__.return_value = False

        with patch("src.gateway.httpx.AsyncClient", return_value=context_manager):
            callback = make_http_progress_callback(message)
            await callback({"type": "tool_start", "message": "Running"})

        _, kwargs = client.post.call_args
        self.assertEqual(kwargs["headers"]["X-Progress-Token"], "progress-secret")
        self.assertEqual(kwargs["json"]["request_id"], "request-1")
        self.assertEqual(
            kwargs["json"]["target"],
            {"chat_id": 10, "message_id": 20},
        )


class TelegramProgressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        telegram_server.progress_edit_state.clear()

    def test_attach_progress_metadata_enables_local_callback(self):
        payload = {"id": "request-1", "metadata": {}}
        update = SimpleNamespace(
            effective_user=SimpleNamespace(language_code="ru"),
            effective_chat=SimpleNamespace(id=10),
        )
        status_message = SimpleNamespace(message_id=20)

        telegram_server.attach_progress_metadata(
            payload=payload,
            update=update,
            status_message=status_message,
        )

        metadata = payload["metadata"]
        self.assertEqual(metadata["status_message_id"], 20)
        self.assertEqual(metadata["progress_request_id"], "request-1")
        self.assertEqual(
            metadata["progress_target"],
            {"chat_id": 10, "message_id": 20},
        )
        self.assertEqual(metadata["progress_locale"], "ru")
        self.assertTrue(metadata["progress_callback_url"])
        self.assertTrue(metadata["progress_callback_token"])

    async def test_internal_endpoint_ignores_debug_event(self):
        request = SimpleNamespace(
            headers={
                "X-Progress-Token": (
                    telegram_server.TELEGRAM_PROGRESS_CALLBACK_TOKEN
                )
            },
            json=AsyncMock(return_value={
                "client_type": "telegram",
                "target": {"chat_id": 10, "message_id": 20},
                "event": {
                    "type": "iteration_started",
                    "message": "Iteration 1/50",
                    "visibility": "debug",
                },
            }),
        )

        with patch.object(
            telegram_server,
            "maybe_edit_progress_message",
            new_callable=AsyncMock,
        ) as edit:
            result = await telegram_server.internal_progress_handler(request)

        self.assertEqual(result["status"], "ignored")
        edit.assert_not_awaited()

    async def test_progress_edit_is_deduplicated(self):
        with patch.object(
            telegram_server,
            "edit_telegram_message_with_retries",
            new_callable=AsyncMock,
        ) as edit:
            await telegram_server.maybe_edit_progress_message(
                chat_id=10,
                message_id=20,
                text="Running tool",
            )
            await asyncio.sleep(0)
            await telegram_server.maybe_edit_progress_message(
                chat_id=10,
                message_id=20,
                text="Running tool",
            )

        edit.assert_awaited_once()

    def test_infrastructure_error_is_rendered_for_telegram(self):
        text = telegram_server.format_agent_error_for_telegram(
            "request failed",
            {
                "agent_status": "error",
                "error": (
                    "Сетевая ошибка LLM на итерации 16: "
                    "LLMTransportError / ConnectError('')"
                ),
                "error_kind": "infrastructure_interruption",
                "iterations": 16,
                "can_resume": True,
            },
            locale_name="ru",
        )

        self.assertIn("инфраструктурной ошибки", text)
        self.assertIn("LLMTransportError / ConnectError", text)
        self.assertIn("Итерация: 16", text)

    def test_http_429_error_type_is_preserved(self):
        summary = telegram_server.extract_error_type_summary(
            "HTTP-ошибка LLM: Ошибка LLM API: 429 - rate limited"
        )
        self.assertEqual(summary, "LLMHTTPError / HTTP 429")

    async def test_send_new_edits_status_and_sends_final_message(self):
        update = SimpleNamespace(effective_chat=SimpleNamespace(id=10))
        status_message = SimpleNamespace(message_id=20)

        with (
            patch.object(
                telegram_server,
                "edit_telegram_message_with_retries",
                new_callable=AsyncMock,
            ) as edit,
            patch.object(
                telegram_server,
                "send_telegram_markdown_reply",
                new_callable=AsyncMock,
            ) as send,
        ):
            await telegram_server.finish_status_or_send_reply(
                update=update,
                status_message=status_message,
                text="Final answer",
                delivery_mode="send_new",
                final_status_text="✅ Готово. Ответ ниже.",
            )

        self.assertEqual(edit.await_args.kwargs["text"], "✅ Готово. Ответ ниже.")
        send.assert_awaited_once_with(update, "Final answer")


if __name__ == "__main__":
    unittest.main()

