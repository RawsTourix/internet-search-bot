import json
import unittest
from unittest.mock import AsyncMock

from src.agent.prompts import AGENT_SYSTEM_PROTOCOL
from src.agent.protocol import AgentAction, dumps_json
from src.core.errors import LLMError, LLMHTTPError
from src.mcp.mcp_client import MCPClient, SessionState


def valid_agent_action_json(final_answer: str = "ok") -> str:
    return dumps_json(
        {
            "type": "agent_action",
            "status": "done",
            "action": "answer",
            "agent_request": None,
            "final_answer": final_answer,
            "question_to_user": None,
            "error_message": None,
        }
    )


class AgentActionContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = object.__new__(MCPClient)

    def test_system_prompt_contains_exact_agent_action_schema(self):
        schema_json = json.dumps(
            AgentAction.model_json_schema(),
            ensure_ascii=False,
            separators=(",", ":"),
        )

        self.assertIn(schema_json, AGENT_SYSTEM_PROTOCOL)

    async def test_repair_uses_schema_and_runtime_context_without_logging_content(self):
        invalid_sentinel = "PRIVATE_INVALID_AGENT_ACTION"
        self.client._call_llm_with_retries = AsyncMock(
            return_value={"content": valid_agent_action_json("repaired")}
        )
        state = SessionState(progress_locale="ru")
        cycle_trace = []
        events = []

        with self.assertLogs("mcp_client", level="WARNING") as captured:
            action = await self.client._parse_or_repair_agent_action(
                invalid_sentinel,
                [],
                state=state,
                session_id="session-1",
                cycle_id="cycle-1",
                progress_callback=events.append,
                cycle_trace=cycle_trace,
            )

        self.assertEqual(action.final_answer, "repaired")
        self.assertNotIn(invalid_sentinel, "\n".join(captured.output))

        call = self.client._call_llm_with_retries.await_args
        repair_payload = json.loads(call.args[0][1]["content"])
        self.assertEqual(repair_payload["schema"], AgentAction.model_json_schema())
        self.assertEqual(repair_payload["invalid_content"], invalid_sentinel)
        self.assertTrue(repair_payload["validation_errors"])
        self.assertNotIn("input", repair_payload["validation_errors"][0])
        self.assertIs(call.kwargs["state"], state)
        self.assertEqual(call.kwargs["session_id"], "session-1")
        self.assertEqual(call.kwargs["cycle_id"], "cycle-1")
        self.assertIs(call.kwargs["cycle_trace"], cycle_trace)
        self.assertTrue(call.kwargs["redact_error_details"])

    async def test_failed_repair_does_not_expose_invalid_responses(self):
        invalid_sentinel = "PRIVATE_ORIGINAL_RESPONSE"
        repaired_sentinel = "PRIVATE_REPAIRED_RESPONSE"
        self.client._call_llm_with_retries = AsyncMock(
            return_value={"content": repaired_sentinel}
        )

        with self.assertRaises(ValueError) as captured:
            await self.client._parse_or_repair_agent_action(
                invalid_sentinel,
                [],
            )

        error_text = str(captured.exception)
        self.assertNotIn(invalid_sentinel, error_text)
        self.assertNotIn(repaired_sentinel, error_text)
        self.assertIsNone(captured.exception.__cause__)
        self.assertTrue(captured.exception.__suppress_context__)


class LLMErrorSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = object.__new__(MCPClient)

    def test_http_error_keeps_raw_body_out_of_string_representation(self):
        provider_sentinel = "PROVIDER_ECHOED_PRIVATE_PROMPT"
        error = LLMHTTPError(500, provider_sentinel)

        self.assertEqual(error.response_text, provider_sentinel)
        self.assertIn("HTTP 500", str(error))
        self.assertNotIn(provider_sentinel, str(error))
        self.assertNotIn(provider_sentinel, repr(error))

    async def test_generic_llm_error_is_not_retried_and_is_redacted(self):
        error_sentinel = "PRIVATE_PROVIDER_RESPONSE"
        self.client.llm_max_retries = 4
        self.client._call_llm = AsyncMock(
            side_effect=LLMError(error_sentinel)
        )
        state = SessionState(progress_locale="en")
        cycle_trace = []
        events = []

        with self.assertLogs("mcp_client", level="ERROR") as captured:
            with self.assertRaises(LLMError):
                await self.client._call_llm_with_retries(
                    [],
                    [],
                    context="Response parsing",
                    state=state,
                    session_id="session-1",
                    cycle_id="cycle-1",
                    progress_callback=events.append,
                    cycle_trace=cycle_trace,
                )

        self.client._call_llm.assert_awaited_once()
        self.assertNotIn(error_sentinel, "\n".join(captured.output))
        self.assertEqual(events[0]["type"], "llm_error")
        self.assertEqual(events[0]["data"]["context"], "Response parsing")
        self.assertEqual(
            events[0]["data"]["error_repr"],
            "LLMError(details omitted)",
        )


class InternalLLMContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = object.__new__(MCPClient)
        self.client._call_llm_with_retries = AsyncMock(
            return_value={"content": "formatted"}
        )

    async def test_final_format_passes_runtime_context_to_retry_pipeline(self):
        state = SessionState(progress_locale="ru")
        cycle_trace = []
        events = []

        result = await self.client._format_final_answer(
            draft_answer="draft",
            client_type=None,
            state=state,
            session_id="session-1",
            cycle_id="cycle-1",
            progress_callback=events.append,
            cycle_trace=cycle_trace,
        )

        self.assertEqual(result, "formatted")
        call = self.client._call_llm_with_retries.await_args
        self.assertIs(call.kwargs["state"], state)
        self.assertEqual(call.kwargs["session_id"], "session-1")
        self.assertEqual(call.kwargs["cycle_id"], "cycle-1")
        self.assertIs(call.kwargs["cycle_trace"], cycle_trace)

