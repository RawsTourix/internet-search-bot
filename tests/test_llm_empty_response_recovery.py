import unittest
from types import SimpleNamespace

from src.core.errors import LLMTransportError
from src.mcp.llm_response_recovery import LLMResponseRecoveryMixin


class _ResponseBase:
    LLM_RUNTIME_METADATA_KEY = "_runtime_llm_metadata"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.events = []
        self.llm_config = SimpleNamespace(
            max_tokens=4096,
            reserved_output_tokens=8192,
        )

    async def _call_llm_with_retries(self, messages, tools, **kwargs):
        self.calls.append({
            "messages": messages,
            "tools": tools,
            **kwargs,
        })
        return self.responses.pop(0)

    def _trace_event(self, trace, event_type, **values):
        event = {"type": event_type, **values}
        trace.append(event)
        self.events.append(event)


class _RecoveryClient(LLMResponseRecoveryMixin, _ResponseBase):
    pass


class LLMEmptyResponseRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_agent_empty_response_retries_with_reserved_budget(self):
        client = _RecoveryClient([
            self._empty("length", 4096),
            {
                "content": None,
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "artifact_create_text", "arguments": "{}"},
                }],
                "_runtime_llm_metadata": {
                    "finish_reason": "tool_calls",
                    "completion_tokens": 128,
                },
            },
        ])
        trace = []

        response = await client._call_llm_with_retries(
            [{"role": "user", "content": "task"}],
            [{"type": "function", "function": {"name": "tool"}}],
            cycle_trace=trace,
            cycle_id="cycle-1",
        )

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(client.calls[1]["max_tokens_override"], 8192)
        self.assertEqual(client.calls[1]["temperature_override"], 0.0)
        self.assertEqual(response["tool_calls"][0]["id"], "call-1")
        self.assertEqual(
            [event["type"] for event in trace],
            ["llm_empty_response_retry", "llm_empty_response_recovered"],
        )

    async def test_non_main_call_keeps_explicit_output_budget(self):
        client = _RecoveryClient([
            self._empty("stop", 0),
            {"content": "result", "tool_calls": []},
        ])

        response = await client._call_llm_with_retries(
            [{"role": "user", "content": "compact"}],
            [],
            max_tokens_override=2048,
        )

        self.assertEqual(response["content"], "result")
        self.assertEqual(client.calls[1]["max_tokens_override"], 2048)

    async def test_repeated_empty_response_becomes_resumable_transport_error(self):
        client = _RecoveryClient([
            self._empty("length", 4096),
            self._empty("length", 8192),
        ])
        trace = []

        with self.assertRaises(LLMTransportError):
            await client._call_llm_with_retries(
                [{"role": "user", "content": "task"}],
                [{"type": "function", "function": {"name": "tool"}}],
                cycle_trace=trace,
            )

        self.assertEqual(
            [event["type"] for event in trace],
            ["llm_empty_response_retry", "llm_empty_response_exhausted"],
        )

    async def test_legacy_function_call_is_normalized_without_retry(self):
        client = _RecoveryClient([{
            "content": None,
            "function_call": {
                "name": "artifact_create_text",
                "arguments": '{"filename":"result.md"}',
            },
        }])

        response = await client._call_llm_with_retries([], [])

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(
            response["tool_calls"][0]["function"]["name"],
            "artifact_create_text",
        )

    @staticmethod
    def _empty(finish_reason: str, completion_tokens: int):
        return {
            "content": None,
            "tool_calls": [],
            "reasoning_content": "hidden reasoning",
            "_runtime_llm_metadata": {
                "finish_reason": finish_reason,
                "prompt_tokens": 100,
                "completion_tokens": completion_tokens,
                "total_tokens": 100 + completion_tokens,
            },
        }


if __name__ == "__main__":
    unittest.main()
