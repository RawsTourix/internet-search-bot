import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from src.agent.protocol import dumps_json
from src.core.errors import LLMHTTPError
from src.memory import (
    InvalidResultHandlingError,
    MemoryConfigType,
    ResultHandling,
)
from src.mcp.mcp_client import LLMConfigType, MCPClient, SessionState
from src.storage import StorageConfigType, StorageError, create_storage_services


class ResultCompactionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.services = create_storage_services(
            StorageConfigType(
                root_dir=str(self.root / "storage"),
                max_in_memory_content_bytes=100_000,
            )
        )
        self.client = MCPClient(
            LLMConfigType(
                api_url="https://example.invalid/v1/chat/completions",
                context_window_tokens=10_000,
                reserved_output_tokens=1_000,
                max_tokens=500,
                context_safety_ratio=0.8,
                context_compaction_target_ratio=0.5,
            ),
            storage_services=self.services,
            memory_config=MemoryConfigType(
                inline_result_max_input_ratio=0.1,
                single_pass_summary_max_input_ratio=0.6,
                result_summary_target_ratio=0.01,
                result_preview_max_chars=80,
            ),
        )
        self.state = SessionState(
            iterations=3,
            progress_locale="en",
        )

    async def asyncTearDown(self):
        await self.client.http_client.aclose()
        self.temporary.cleanup()

    async def _process(
        self,
        raw,
        *,
        handling=ResultHandling.AUTO,
        outer_tool_name="search",
        effective_tool_name="search",
        outer_arguments=None,
        effective_arguments=None,
        tool_call_id="call-1",
        messages=None,
        trace=None,
        events=None,
    ):
        outer_arguments = outer_arguments or {}
        effective_arguments = effective_arguments or {"query": "python"}
        messages = messages if messages is not None else [
            {"role": "system", "content": "system"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": tool_call_id}],
            },
        ]
        trace = trace if trace is not None else []
        events = events if events is not None else []
        if outer_tool_name == "mcp_call_tool":
            raw_tool_result_text = dumps_json({
                "type": "tool_result",
                "trusted": False,
                "tool_name": effective_tool_name,
                "content": raw,
            })
        else:
            raw_tool_result_text = raw
        tool_payload = self.client._tool_result_payload(
            outer_tool_name,
            raw_tool_result_text,
        )
        outcome = await self.client._process_tool_result_for_context(
            outer_tool_name=outer_tool_name,
            effective_tool_name=effective_tool_name,
            tool_call_id=tool_call_id,
            outer_arguments=outer_arguments,
            effective_arguments=effective_arguments,
            raw_tool_result_text=raw_tool_result_text,
            tool_payload=tool_payload,
            result_handling=handling,
            messages_for_llm=messages,
            original_user_request="research python",
            session_id="session-1",
            cycle_id="cycle-1",
            state=self.state,
            cycle_trace=trace,
            progress_callback=events.append,
        )
        return outcome, messages, trace, events

    async def test_schema_and_remote_arguments_keep_result_handling_local(self):
        schema = next(
            item
            for item in self.client._format_tools_for_llm()
            if item["function"]["name"] == "mcp_call_tool"
        )["function"]["parameters"]

        self.assertIn("result_handling", schema["properties"])
        self.assertEqual(
            schema["required"],
            ["tool_name", "arguments"],
        )

        remote_result = SimpleNamespace(
            content=[SimpleNamespace(text="remote result")]
        )
        self.client.server_manager = SimpleNamespace(
            call_tool=AsyncMock(return_value=remote_result)
        )
        payload = await self.client._manager_call_tool({
            "tool_name": "web_search",
            "arguments": {"query": "python"},
            "result_handling": "compact",
        })

        self.client.server_manager.call_tool.assert_awaited_once_with(
            "web_search",
            {"query": "python"},
        )
        self.assertEqual(payload["content"], "remote result")

    async def test_invalid_result_handling_does_not_call_remote_tool(self):
        self.client.server_manager = SimpleNamespace(
            call_tool=AsyncMock()
        )

        with self.assertRaises(InvalidResultHandlingError):
            await self.client._manager_call_tool({
                "tool_name": "web_search",
                "arguments": {},
                "result_handling": "unsafe",
            })

        self.client.server_manager.call_tool.assert_not_awaited()

    async def test_list_tools_rejects_aggregate_schemas(self):
        schema = next(
            item
            for item in self.client._format_tools_for_llm()
            if item["function"]["name"] == "mcp_list_tools"
        )["function"]["parameters"]
        include_schemas = schema["properties"]["include_schemas"]
        self.assertFalse(include_schemas["const"])

        self.client.server_manager = SimpleNamespace(
            list_tools=Mock()
        )
        with self.assertRaisesRegex(
            ValueError,
            "mcp_get_tool_schema",
        ):
            await self.client._manager_list_tools({
                "include_schemas": True,
            })
        self.client.server_manager.list_tools.assert_not_called()

    async def test_control_plane_schema_bypasses_llm_compaction(self):
        raw = dumps_json({
            "type": "mcp_tool_schema",
            "tool": {
                "name": "find_events",
                "description": "x" * 2_000,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "place": {"type": "string"},
                    },
                },
            },
        })
        self.client.result_compaction_service.persist_result = AsyncMock()
        self.client._call_llm_with_retries = AsyncMock()

        outcome, messages, trace, _ = await self._process(
            raw,
            outer_tool_name="mcp_get_tool_schema",
            effective_tool_name="mcp_get_tool_schema",
            effective_arguments={"tool_name": "find_events"},
        )

        self.assertEqual(outcome.decision.representation, "inline")
        self.assertEqual(
            outcome.decision.reason,
            "control_plane_required_inline",
        )
        self.assertTrue(outcome.decision.runtime_override)
        self.client.result_compaction_service.persist_result.assert_not_awaited()
        self.client._call_llm_with_retries.assert_not_awaited()
        self.assertEqual(
            json.loads(messages[-1]["content"])["tool"]["name"],
            "find_events",
        )
        self.assertIn(
            "tool_result_full",
            [event["type"] for event in trace],
        )

    async def test_unsafe_control_plane_result_is_not_llm_compacted(self):
        raw = dumps_json({
            "type": "mcp_tools",
            "tools": [{"description": "x" * 12_000}],
        })
        self.client.result_compaction_service.persist_result = AsyncMock()
        self.client._call_llm_with_retries = AsyncMock()

        outcome, messages, trace, _ = await self._process(
            raw,
            outer_tool_name="mcp_list_tools",
            effective_tool_name="mcp_list_tools",
        )

        visible = json.loads(messages[-1]["content"])
        self.assertTrue(outcome.persistence_failed)
        self.assertEqual(
            outcome.decision.reason,
            "control_plane_result_exceeds_hard_context",
        )
        self.assertEqual(
            visible["type"],
            "tool_result_processing_error",
        )
        self.assertFalse(visible["retry_recommended"])
        self.client.result_compaction_service.persist_result.assert_not_awaited()
        self.client._call_llm_with_retries.assert_not_awaited()
        self.assertIn(
            "tool_result_processing_error",
            [event["type"] for event in trace],
        )

    async def test_inline_result_is_not_persisted_or_summarized(self):
        self.client.result_compaction_service.persist_result = AsyncMock()
        self.client._call_llm_with_retries = AsyncMock()
        raw = "small result"

        outcome, messages, trace, _ = await self._process(raw)

        self.assertEqual(outcome.decision.representation, "inline")
        self.client.result_compaction_service.persist_result.assert_not_awaited()
        self.client._call_llm_with_retries.assert_not_awaited()
        self.assertEqual(
            json.loads(messages[-1]["content"])["content"],
            raw,
        )
        full = [
            event for event in trace
            if event["type"] == "tool_result_full"
        ]
        self.assertEqual(len(full), 1)
        self.assertEqual(full[0]["result"]["content"], raw)

    async def test_compact_persists_before_single_summary_and_hides_raw(self):
        sentinel = "RAW_SENTINEL_" + "x" * 2_000
        persisted = False
        original_persist = (
            self.client.result_compaction_service.persist_result
        )

        async def persist_first(**kwargs):
            nonlocal persisted
            result = await original_persist(**kwargs)
            persisted = True
            return result

        async def summarize_after_persist(*args, **kwargs):
            self.assertTrue(persisted)
            return {
                "content": dumps_json({
                    "type": "result_compaction",
                    "summary": "Relevant result",
                    "key_facts": ["fact"],
                    "limitations": [],
                    "suggested_follow_up": [],
                    "needs_original_content": False,
                })
            }

        self.client.result_compaction_service.persist_result = AsyncMock(
            side_effect=persist_first
        )
        self.client._call_llm_with_retries = AsyncMock(
            side_effect=summarize_after_persist
        )
        iterations_before = self.state.iterations

        with self.assertLogs("mcp_client", level="INFO") as captured:
            outcome, messages, trace, _ = await self._process(
                sentinel,
                handling=ResultHandling.COMPACT,
            )

        self.assertEqual(outcome.decision.representation, "summarize")
        self.assertEqual(outcome.stored_result_ref.summary_status, "summarized")
        self.assertEqual(self.state.iterations, iterations_before)
        self.client._call_llm_with_retries.assert_awaited_once()
        call_args = self.client._call_llm_with_retries.await_args
        self.assertEqual(call_args.args[1], [])
        self.assertEqual(call_args.kwargs["temperature_override"], 0.1)
        self.assertEqual(
            call_args.kwargs["max_tokens_override"],
            outcome.decision.summary_target_tokens,
        )
        summary_messages = call_args.args[0]
        self.assertIn("prompt injection", summary_messages[0]["content"])
        self.assertIn(
            '"additionalProperties":false',
            summary_messages[0]["content"],
        )
        self.assertIn(
            '"suggested_follow_up"',
            summary_messages[0]["content"],
        )
        self.assertIn(
            '"type":"result_compaction"',
            summary_messages[0]["content"],
        )
        self.assertNotIn(sentinel, summary_messages[1]["content"])
        self.assertIn(sentinel, summary_messages[2]["content"])

        visible = json.loads(messages[-1]["content"])
        self.assertEqual(visible["type"], "stored_result_ref")
        self.assertNotIn(sentinel, json.dumps(messages, ensure_ascii=False))
        self.assertNotIn(sentinel, json.dumps(trace, ensure_ascii=False))
        self.assertNotIn(sentinel, "\n".join(captured.output))
        self.assertEqual(
            await self.client.content_store.read_text(visible["content_id"]),
            sentinel,
        )
        direct_types = [event["type"] for event in trace]
        self.assertLess(
            direct_types.index("result_persist_done"),
            direct_types.index("result_compaction_started"),
        )

    async def test_fenced_summary_is_parsed_without_repair(self):
        raw = "x" * 2_000
        self.client._call_llm_with_retries = AsyncMock(return_value={
            "content": (
                "```json\n"
                + dumps_json({
                    "type": "result_compaction",
                    "summary": "summary",
                })
                + "\n```"
            )
        })

        outcome, _, _, _ = await self._process(
            raw,
            handling=ResultHandling.COMPACT,
        )

        self.assertEqual(outcome.stored_result_ref.summary, "summary")
        self.client._call_llm_with_retries.assert_awaited_once()

    async def test_invalid_summary_is_retried_once_with_larger_budget(self):
        raw = "x" * 2_000
        self.client._call_llm_with_retries = AsyncMock(side_effect=[
            {"content": "not-json"},
            {
                "content": dumps_json({
                    "type": "result_compaction",
                    "summary": "repaired summary",
                }),
            },
        ])

        outcome, _, trace, _ = await self._process(
            raw,
            handling=ResultHandling.COMPACT,
        )

        self.assertFalse(outcome.summary_failed)
        self.assertEqual(
            outcome.stored_result_ref.summary,
            "repaired summary",
        )
        self.assertEqual(
            self.client._call_llm_with_retries.await_count,
            2,
        )
        repair_call = self.client._call_llm_with_retries.await_args_list[1]
        self.assertEqual(
            repair_call.kwargs["max_tokens_override"],
            self.client.llm_config.max_tokens,
        )
        self.assertEqual(
            repair_call.kwargs["temperature_override"],
            0.0,
        )
        self.assertIn(
            "result_compaction_retry",
            [event["type"] for event in trace],
        )

    async def test_invalid_summary_falls_back_without_losing_original(self):
        raw = "x" * 2_000
        self.client._call_llm_with_retries = AsyncMock(
            return_value={"content": "not-json"}
        )

        outcome, messages, trace, events = await self._process(
            raw,
            handling=ResultHandling.COMPACT,
        )

        visible = json.loads(messages[-1]["content"])
        self.assertTrue(outcome.summary_failed)
        self.assertEqual(visible["summary_status"], "failed")
        self.assertTrue(visible["needs_retrieval"])
        self.assertEqual(
            await self.client.content_store.read_text(visible["content_id"]),
            raw,
        )
        self.assertIn(
            "result_compaction_failed",
            [event["type"] for event in trace],
        )
        failed_event = next(
            event for event in events
            if event["type"] == "result_compaction_failed"
        )
        self.assertEqual(
            failed_event["data"]["validation_issue_count"],
            1,
        )
        self.assertEqual(
            failed_event["data"]["validation_issues"],
            [{"type": "json_invalid", "location": ["$"]}],
        )
        self.assertEqual(
            self.client._call_llm_with_retries.await_count,
            2,
        )

    async def test_validation_diagnostics_redact_untrusted_field_names(self):
        raw = "x" * 2_000
        untrusted_field = "RAW_RESPONSE_SENTINEL"
        self.client._call_llm_with_retries = AsyncMock(return_value={
            "content": dumps_json({
                "summary": "summary",
                untrusted_field: "must not be logged",
            })
        })

        with self.assertLogs("mcp_client", level="INFO") as captured:
            outcome, _, trace, events = await self._process(
                raw,
                handling=ResultHandling.COMPACT,
            )

        self.assertTrue(outcome.summary_failed)
        failed_event = next(
            event for event in events
            if event["type"] == "result_compaction_failed"
        )
        self.assertEqual(
            failed_event["data"]["validation_issues"],
            [{
                "type": "extra_forbidden",
                "location": ["<untrusted-field>"],
            }],
        )
        serialized = (
            json.dumps(trace, ensure_ascii=False)
            + json.dumps(events, ensure_ascii=False)
            + "\n".join(captured.output)
        )
        self.assertNotIn(untrusted_field, serialized)

    async def test_summary_http_failure_redacts_provider_response(self):
        raw = "x" * 2_000
        echoed_sentinel = "PROVIDER_ECHOED_RAW_SENTINEL"
        self.client.llm_max_retries = 0
        self.client._call_llm = AsyncMock(
            side_effect=LLMHTTPError(
                500,
                f"provider echoed {echoed_sentinel}",
            )
        )

        with self.assertLogs("mcp_client", level="INFO") as captured:
            outcome, _, trace, events = await self._process(
                raw,
                handling=ResultHandling.COMPACT,
            )

        self.assertTrue(outcome.summary_failed)
        serialized = (
            json.dumps(trace, ensure_ascii=False)
            + json.dumps(events, ensure_ascii=False)
            + "\n".join(captured.output)
        )
        self.assertNotIn(echoed_sentinel, serialized)
        llm_error = next(
            event for event in events
            if event["type"] == "llm_error"
        )
        self.assertEqual(
            llm_error["data"]["error_repr"],
            "LLMHTTPError(details omitted)",
        )

    async def test_store_only_and_oversized_do_not_call_llm(self):
        self.client._call_llm_with_retries = AsyncMock()

        store_only, _, _, _ = await self._process(
            "small",
            handling=ResultHandling.STORE_ONLY,
        )
        oversized, _, _, _ = await self._process(
            "z" * 12_000,
            handling=ResultHandling.AUTO,
            tool_call_id="call-2",
        )

        self.assertEqual(
            store_only.stored_result_ref.summary_status,
            "store_only",
        )
        self.assertEqual(
            oversized.stored_result_ref.summary_status,
            "oversized",
        )
        self.assertTrue(store_only.stored_result_ref.preview)
        self.assertTrue(oversized.stored_result_ref.needs_retrieval)
        self.client._call_llm_with_retries.assert_not_awaited()

    async def test_prefer_inline_is_overridden_for_unsafe_result(self):
        self.client._call_llm_with_retries = AsyncMock(return_value={
            "content": dumps_json({
                "type": "result_compaction",
                "summary": "summary",
            })
        })

        outcome, _, _, _ = await self._process(
            "x" * 2_000,
            handling=ResultHandling.PREFER_INLINE,
        )

        self.assertNotEqual(outcome.decision.representation, "inline")
        self.assertTrue(outcome.decision.runtime_override)

    async def test_manager_wrapper_uses_canonical_remote_content_and_name(self):
        raw = '{"remote":true}'
        self.client._call_llm_with_retries = AsyncMock()

        outcome, messages, trace, _ = await self._process(
            raw,
            handling=ResultHandling.STORE_ONLY,
            outer_tool_name="mcp_call_tool",
            effective_tool_name="web_search",
            outer_arguments={
                "tool_name": "web_search",
                "arguments": {"query": "python"},
                "result_handling": "store_only",
            },
            effective_arguments={"query": "python"},
        )

        ref = json.loads(messages[-1]["content"])
        self.assertEqual(ref["tool_name"], "web_search")
        self.assertEqual(
            await self.client.content_store.read_text(ref["content_id"]),
            raw,
        )
        stored_event = next(
            event for event in trace
            if event["type"] == "tool_result_stored"
        )
        self.assertEqual(stored_event["tool_name"], "web_search")
        self.assertEqual(
            outcome.content_ref.source_name,
            "web_search",
        )

    async def test_multiple_calls_keep_one_ordered_tool_message_per_call(self):
        self.client._call_llm_with_retries = AsyncMock()
        messages = [{"role": "system", "content": "system"}]
        trace = []
        events = []

        await self._process(
            "inline",
            tool_call_id="call-1",
            messages=messages,
            trace=trace,
            events=events,
        )
        await self._process(
            "stored",
            handling=ResultHandling.STORE_ONLY,
            tool_call_id="call-2",
            messages=messages,
            trace=trace,
            events=events,
        )

        tool_messages = [
            message for message in messages
            if message.get("role") == "tool"
        ]
        self.assertEqual(
            [message["tool_call_id"] for message in tool_messages],
            ["call-1", "call-2"],
        )
        self.assertEqual(len(tool_messages), 2)

    async def test_process_query_integrates_compaction_without_extra_iteration(self):
        raw = "LOOP_RAW_SENTINEL_" + "x" * 2_000
        tool_call = {
            "id": "call-loop",
            "type": "function",
            "function": {
                "name": "mcp_call_tool",
                "arguments": dumps_json({
                    "tool_name": "web_search",
                    "arguments": {"query": "python"},
                    "result_handling": "compact",
                }),
            },
        }
        summary_response = {
            "content": dumps_json({
                "type": "result_compaction",
                "summary": "Loop summary",
                "key_facts": ["loop fact"],
            })
        }
        final_response = {
            "content": dumps_json({
                "type": "agent_action",
                "status": "done",
                "action": "answer",
                "agent_request": None,
                "final_answer": "Finished",
                "question_to_user": None,
                "error_message": None,
            })
        }
        self.client._call_llm_with_retries = AsyncMock(side_effect=[
            {"content": None, "tool_calls": [tool_call]},
            summary_response,
            final_response,
        ])
        self.client._call_registered_tool = AsyncMock(return_value=SimpleNamespace(
            content=[SimpleNamespace(text=raw)]
        ))
        self.client._archive_agent_cycle = Mock()

        result = await self.client.process_query(
            "research python",
            session_id="loop-session",
            progress_locale="en",
        )

        self.assertEqual(result.content, "Finished")
        self.assertEqual(result.iterations, 2)
        self.assertEqual(self.client._call_llm_with_retries.await_count, 3)
        summary_call = self.client._call_llm_with_retries.await_args_list[1]
        self.assertEqual(summary_call.args[1], [])
        archive_messages = (
            self.client._archive_agent_cycle.call_args.kwargs[
                "messages_for_llm"
            ]
        )
        archive_trace = (
            self.client._archive_agent_cycle.call_args.kwargs["cycle_trace"]
        )
        self.assertNotIn(
            raw,
            json.dumps(archive_messages, ensure_ascii=False),
        )
        self.assertNotIn(
            raw,
            json.dumps(archive_trace, ensure_ascii=False),
        )
        tool_messages = [
            message for message in archive_messages
            if message.get("role") == "tool"
        ]
        self.assertEqual(len(tool_messages), 1)
        self.assertEqual(tool_messages[0]["tool_call_id"], "call-loop")
        result_stage_types = [
            event["type"]
            for event in result.progress_events
            if event["type"] in {
                "result_persist_started",
                "result_persist_done",
                "result_compaction_started",
                "result_compaction_done",
                "tool_done",
            }
        ]
        self.assertEqual(result_stage_types, [
            "result_persist_started",
            "result_persist_done",
            "result_compaction_started",
            "result_compaction_done",
            "tool_done",
        ])

    async def test_process_query_keeps_tool_done_when_only_summary_fails(self):
        raw = "STORED_RAW_SENTINEL_" + "x" * 2_000
        tool_call = {
            "id": "call-summary-failed",
            "type": "function",
            "function": {
                "name": "mcp_call_tool",
                "arguments": dumps_json({
                    "tool_name": "web_search",
                    "arguments": {"query": "python"},
                    "result_handling": "compact",
                }),
            },
        }
        final_response = {
            "content": dumps_json({
                "type": "agent_action",
                "status": "done",
                "action": "answer",
                "agent_request": None,
                "final_answer": "Finished from stored reference",
                "question_to_user": None,
                "error_message": None,
            })
        }
        self.client._call_llm_with_retries = AsyncMock(side_effect=[
            {"content": None, "tool_calls": [tool_call]},
            RuntimeError("summary unavailable"),
            final_response,
        ])
        self.client._call_registered_tool = AsyncMock(
            return_value=SimpleNamespace(
                content=[SimpleNamespace(text=raw)]
            )
        )
        self.client._archive_agent_cycle = Mock()

        result = await self.client.process_query(
            "research python",
            session_id="summary-failed-session",
            progress_locale="en",
        )

        self.assertEqual(
            result.content,
            "Finished from stored reference",
        )
        progress_types = [event["type"] for event in result.progress_events]
        self.assertIn("result_persist_done", progress_types)
        self.assertIn("result_compaction_failed", progress_types)
        self.assertIn("tool_done", progress_types)
        self.assertNotIn("tool_error", progress_types)

        archive_trace = (
            self.client._archive_agent_cycle.call_args.kwargs["cycle_trace"]
        )
        self.assertIn(
            "tool_result_stored",
            [event["type"] for event in archive_trace],
        )
        self.assertNotIn(
            raw,
            json.dumps(archive_trace, ensure_ascii=False),
        )

    async def test_process_query_marks_unavailable_result_as_tool_error(self):
        raw = "LOST_RAW_SENTINEL_" + "x" * 2_000
        tool_call = {
            "id": "call-lost",
            "type": "function",
            "function": {
                "name": "mcp_call_tool",
                "arguments": dumps_json({
                    "tool_name": "web_search",
                    "arguments": {"query": "python"},
                    "result_handling": "compact",
                }),
            },
        }
        final_response = {
            "content": dumps_json({
                "type": "agent_action",
                "status": "done",
                "action": "answer",
                "agent_request": None,
                "final_answer": "Finished with limitation",
                "question_to_user": None,
                "error_message": None,
            })
        }
        self.client._call_llm_with_retries = AsyncMock(side_effect=[
            {"content": None, "tool_calls": [tool_call]},
            final_response,
        ])
        self.client._call_registered_tool = AsyncMock(
            return_value=SimpleNamespace(
                content=[SimpleNamespace(text=raw)]
            )
        )
        self.client.result_compaction_service.persist_result = AsyncMock(
            side_effect=StorageError("disk unavailable")
        )
        self.client._archive_agent_cycle = Mock()

        result = await self.client.process_query(
            "research python",
            session_id="lost-session",
            progress_locale="en",
        )

        self.assertEqual(result.content, "Finished with limitation")
        progress_types = [event["type"] for event in result.progress_events]
        self.assertIn("result_persist_failed", progress_types)
        self.assertIn("tool_error", progress_types)
        self.assertNotIn("tool_done", progress_types)
        tool_error = next(
            event
            for event in result.progress_events
            if event["type"] == "tool_error"
        )
        self.assertIn("unavailable", tool_error["message"])
        self.assertFalse(tool_error["data"]["result_available"])

        archive_trace = (
            self.client._archive_agent_cycle.call_args.kwargs["cycle_trace"]
        )
        self.assertIn(
            "tool_result_processing_error",
            [event["type"] for event in archive_trace],
        )
        self.assertNotIn(
            raw,
            json.dumps(archive_trace, ensure_ascii=False),
        )

    async def test_persistence_failure_uses_safe_inline_or_bounded_error(self):
        self.client.result_compaction_service.persist_result = AsyncMock(
            side_effect=StorageError("disk unavailable")
        )

        safe, safe_messages, safe_trace, _ = await self._process(
            "small",
            handling=ResultHandling.COMPACT,
        )
        unsafe, unsafe_messages, unsafe_trace, _ = await self._process(
            "x" * 12_000,
            handling=ResultHandling.AUTO,
            tool_call_id="call-2",
        )
        store_only, store_only_messages, _, _ = await self._process(
            "small but explicitly stored",
            handling=ResultHandling.STORE_ONLY,
            tool_call_id="call-3",
        )

        self.assertTrue(safe.persistence_failed)
        self.assertEqual(safe.decision.representation, "inline")
        self.assertEqual(
            json.loads(safe_messages[-1]["content"])["content"],
            "small",
        )
        self.assertIn(
            "result_persistence_inline_fallback",
            [event["type"] for event in safe_trace],
        )
        self.assertTrue(unsafe.persistence_failed)
        unsafe_payload = json.loads(unsafe_messages[-1]["content"])
        self.assertEqual(
            unsafe_payload["type"],
            "tool_result_processing_error",
        )
        self.assertFalse(unsafe_payload["retry_recommended"])
        self.assertTrue(store_only.persistence_failed)
        store_only_payload = json.loads(
            store_only_messages[-1]["content"]
        )
        self.assertEqual(
            store_only_payload["type"],
            "tool_result_processing_error",
        )
        self.assertFalse(store_only_payload["result_available"])
        self.assertEqual(store_only.decision.representation, "store_only")
        self.assertNotIn(
            "x" * 100,
            json.dumps(unsafe_trace, ensure_ascii=False),
        )


if __name__ == "__main__":
    unittest.main()
