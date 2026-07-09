import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.agent.progress_messages import (
    PROGRESS_MESSAGES,
    normalize_progress_locale,
    progress_text,
)
from src.agent.protocol import ProgressEvent, dumps_json
from src.core.models import ClientType, UnifiedMessage
from src.gateway import (
    is_allowed_progress_callback_url,
    make_http_progress_callback,
)
from src.core.errors import LLMHTTPError, LLMTimeoutError, LLMTransportError
from src.mcp.mcp_client import (
    FinalProcessingDecision,
    FinalProcessingMode,
    MCPClient,
    MCPToolBinding,
    SessionState,
)
from src.servers.telegram import telegram_server


class ProgressRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = object.__new__(MCPClient)
        self.client.tool_registry = {}
        self.client.manager_tools = self.client._build_manager_tools()

    def test_progress_text_uses_localized_default_for_empty_tool_name(self):
        self.assertEqual(
            progress_text("mcp_get_tool_schema", locale_name="ru", tool_name=None),
            "📋 Проверяю схему инструмента…",
        )
        self.assertEqual(
            progress_text("mcp_call_tool", locale_name="en", tool_name=""),
            "🔧 Running tool…",
        )

    def test_progress_locale_normalization_uses_available_catalogs(self):
        self.assertEqual(normalize_progress_locale(None), "ru")
        self.assertEqual(normalize_progress_locale("ru-RU"), "ru")
        self.assertEqual(normalize_progress_locale("en_GB"), "en")
        self.assertEqual(normalize_progress_locale("unknown"), "ru")

        with patch.dict(PROGRESS_MESSAGES, {"de": {}}, clear=False):
            self.assertEqual(normalize_progress_locale("de-DE"), "de")

    def test_manager_tool_progress_arguments_are_mapped_declaratively(self):
        self.assertEqual(
            self.client._tool_start_message(
                "mcp_get_tool_schema",
                {"tool_name": "events"},
                progress_locale="en",
            ),
            "📋 Checking schema for events…",
        )
        self.assertEqual(
            self.client._tool_start_message(
                "mcp_call_tool",
                {"tool_name": None},
                progress_locale="ru",
            ),
            "🔧 Запускаю инструмент…",
        )

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

    def test_final_processing_mode_skips_short_answers_without_tools(self):
        self.client.llm_config = SimpleNamespace(final_audit=True)
        state = SessionState(iterations=1)

        decision = self.client._select_final_processing_mode(
            result_text="Привет! Чем могу помочь?",
            state=state,
            cycle_trace=[],
        )

        self.assertEqual(decision.mode, FinalProcessingMode.SKIP)
        self.assertEqual(decision.reason, "short_no_tools")

    def test_final_processing_mode_uses_structural_risk_signals(self):
        self.client.llm_config = SimpleNamespace(final_audit=True)
        state = SessionState(iterations=2, tools_used=["search"])
        cycle_trace = [
            {
                "type": "tool_result_full",
                "result": {
                    "content": dumps_json({
                        "data": {
                            "count": 0,
                            "items": [],
                        },
                    }),
                },
            },
        ]

        decision = self.client._select_final_processing_mode(
            result_text="Ничего не найдено.",
            state=state,
            cycle_trace=cycle_trace,
        )

        self.assertEqual(decision.mode, FinalProcessingMode.STRICT_GROUNDED)
        self.assertEqual(decision.reason, "risky_tool_workflow")

    def test_final_evidence_pack_preserves_full_tool_result(self):
        state = SessionState(tools_used=["search"])
        tool_result = {
            "content": dumps_json({
                "data": {
                    "id": 1,
                    "title": "A",
                    "custom_payload": {"nested": True},
                },
            }),
        }
        cycle_trace = [
            {
                "type": "tool_result_full",
                "tool_name": "search",
                "tool_call_id": "call-1",
                "result": tool_result,
            },
            {
                "type": "tool_error",
                "tool_name": "details",
                "error": "timeout",
            },
        ]

        evidence = self.client._build_final_evidence_pack(
            original_user_request="find places",
            state=state,
            cycle_trace=cycle_trace,
        )

        self.assertEqual(evidence["tool_results"][0]["result"], tool_result)
        self.assertEqual(evidence["tool_errors"][0]["error"], "timeout")
        self.assertEqual(evidence["limitations"][0]["type"], "tool_errors_present")

    async def test_process_final_answer_runs_grounding_before_formatting(self):
        self.client._ground_final_answer = AsyncMock(return_value="grounded")
        self.client._format_final_answer = AsyncMock(return_value="formatted")
        evidence_pack = {"type": "final_evidence_pack"}

        result = await self.client._process_final_answer(
            draft_answer="draft",
            client_type=ClientType.WEB,
            decision=FinalProcessingDecision(
                FinalProcessingMode.GROUNDED,
                "tools_used",
            ),
            evidence_pack=evidence_pack,
        )

        self.assertEqual(result, "formatted")
        self.client._ground_final_answer.assert_awaited_once()
        self.client._format_final_answer.assert_awaited_once()
        self.assertEqual(
            self.client._ground_final_answer.await_args.kwargs["evidence_pack"],
            evidence_pack,
        )
        self.assertEqual(
            self.client._format_final_answer.await_args.kwargs["draft_answer"],
            "grounded",
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

    async def test_emit_progress_event_preserves_callback_and_trace_payload(self):
        state = SessionState(progress_locale="en", iterations=2)
        events = []
        cycle_trace = []

        await self.client._emit_progress_event(
            state=state,
            session_id="session-1",
            cycle_id="cycle-1",
            progress_callback=events.append,
            cycle_trace=cycle_trace,
            event_type="iteration_started",
            visibility="debug",
            message_kwargs={"iteration": 2, "max_iterations": 8},
        )

        self.assertEqual(events, state.progress_events)
        self.assertEqual(events[0]["type"], "iteration_started")
        self.assertEqual(events[0]["message"], "Iteration 2/8")
        self.assertEqual(events[0]["visibility"], "debug")
        self.assertEqual(events[0]["session_id"], "session-1")
        self.assertEqual(events[0]["cycle_id"], "cycle-1")
        self.assertEqual(cycle_trace[0]["type"], "progress_event")
        self.assertEqual(cycle_trace[0]["progress_event"], events[0])

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

    async def test_llm_http_404_non_retryable_emits_no_retry_error(self):
        self.client.llm_max_retries = 4
        self.client.llm_retryable_http_statuses = {429, 500, 502, 503, 504}
        self.client._call_llm = AsyncMock(
            side_effect=LLMHTTPError(404, "not found")
        )
        state = SessionState(progress_locale="ru")
        events = []

        with self.assertRaises(LLMHTTPError):
            await self.client._call_llm_with_retries(
                [],
                [],
                context="Итерация 1",
                state=state,
                session_id="session-1",
                cycle_id="cycle-1",
                progress_callback=events.append,
                cycle_trace=[],
            )

        self.client._call_llm.assert_awaited_once()
        self.assertEqual(events[0]["type"], "llm_error")
        self.assertIn("LLM HTTP 404", events[0]["message"])
        self.assertIn("Повтор не выполняется", events[0]["message"])
        self.assertNotIn("Повторы исчерпаны", events[0]["message"])
        self.assertEqual(events[0]["data"]["attempt"], 1)
        self.assertEqual(events[0]["data"]["max_attempts"], 5)

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

    def test_llm_http_404_is_rendered_as_configuration_error(self):
        text = telegram_server.format_agent_error_for_telegram(
            "request failed",
            {
                "agent_status": "error",
                "error": "HTTP-ошибка LLM на итерации 1: LLM API: 404 - not found",
                "error_kind": "llm_configuration_error",
                "iterations": 1,
                "can_resume": False,
            },
            locale_name="ru",
        )

        self.assertIn("ошибки конфигурации LLM", text)
        self.assertIn("LLMHTTPError / HTTP 404", text)
        self.assertIn("Итерация: 1", text)
        self.assertIn("Проверь API URL", text)
        self.assertNotIn("можно продолжить позже", text)

    async def test_send_new_preserves_status_and_sends_final_message(self):
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
            )

        edit.assert_not_awaited()
        send.assert_awaited_once_with(update, "Final answer")


if __name__ == "__main__":
    unittest.main()
