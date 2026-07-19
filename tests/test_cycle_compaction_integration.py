import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock

from src.agent.protocol import dumps_json
from src.memory import (
    CycleContextLimitError,
    CycleWorkingMemory,
    CycleWorkingState,
    MemoryConfigType,
    parse_cycle_working_memory_message,
)
from src.mcp.mcp_client import LLMConfigType, MCPClient, SessionState
from src.runtime import ActiveAgentCycle
from src.storage import StorageConfigType, StorageError, create_storage_services


def valid_compaction_response(summary="working summary"):
    return {
        "content": dumps_json({
            "type": "cycle_compaction_result",
            "summary": summary,
            "working_state": {
                "current_goal": "continue the task",
            },
        })
    }


class CycleCompactionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.services = create_storage_services(
            StorageConfigType(root_dir=str(self.root / "storage"))
        )
        self.client = MCPClient(
            LLMConfigType(
                api_url="https://example.invalid/v1/chat/completions",
                context_window_tokens=40_000,
                reserved_output_tokens=2_000,
                max_tokens=1_000,
                context_safety_ratio=0.60,
                context_compaction_target_ratio=0.35,
            ),
            storage_services=self.services,
            memory_config=MemoryConfigType(
                cycle_compaction_keep_recent_blocks=2,
                cycle_compaction_max_passes=3,
            ),
        )
        self.state = SessionState(progress_locale="en")

    async def asyncTearDown(self):
        await self.client.http_client.aclose()
        self.temporary.cleanup()

    def _active_cycle(self, *, block_count=8, block_chars=3_000):
        messages = [
            {"role": "system", "content": "system"},
            {
                "role": "user",
                "content": dumps_json({
                    "type": "user_request",
                    "user_request": "complete the task",
                }),
            },
        ]
        for index in range(block_count):
            messages.append({
                "role": "assistant",
                "content": f"OLD_BLOCK_{index}_" + "x" * block_chars,
            })
        return ActiveAgentCycle(
            cycle_id="cycle-1",
            session_id="session-1",
            original_user_request="complete the task",
            messages_for_llm=messages,
            cycle_trace=[],
            original_user_message_index=1,
        )

    async def _compact(self, cycle, events=None):
        events = events if events is not None else []
        return await self.client._compact_context_if_needed(
            active_cycle=cycle,
            state=self.state,
            session_id="session-1",
            progress_callback=events.append,
        )

    async def test_atomic_replacement_and_multiple_passes(self):
        cycle = self._active_cycle()
        events = []
        self.state.iterations = 7
        self.client._call_llm_with_retries = AsyncMock(
            return_value=valid_compaction_response()
        )
        before_messages = list(cycle.messages_for_llm)

        outcome = await self._compact(cycle, events)

        self.assertTrue(outcome.changed)
        self.assertGreater(outcome.before_tokens, outcome.after_tokens)
        self.assertGreaterEqual(outcome.passes_completed, 1)
        self.assertLessEqual(outcome.passes_completed, 3)
        self.assertEqual(
            cycle.working_memory.generation,
            outcome.passes_completed,
        )

        memories = [
            parse_cycle_working_memory_message(message)
            for message in cycle.messages_for_llm
            if parse_cycle_working_memory_message(message) is not None
        ]
        self.assertEqual(len(memories), 1)
        self.assertEqual(cycle.messages_for_llm[1], before_messages[1])
        self.assertEqual(
            [event["type"] for event in events].count(
                "cycle_compaction_started"
            ),
            1,
        )
        self.assertEqual(
            [event["type"] for event in events].count(
                "cycle_compaction_done"
            ),
            1,
        )
        self.assertEqual(
            self.client._call_llm_with_retries.await_count,
            outcome.passes_completed,
        )
        self.assertEqual(self.state.iterations, 7)
        for call in self.client._call_llm_with_retries.await_args_list:
            self.assertEqual(call.args[1], [])
            self.assertEqual(call.kwargs["temperature_override"], 0.1)
            self.assertEqual(
                call.kwargs["max_tokens_override"],
                self.client._cycle_compactor_output_tokens(),
            )
        compact_events = [
            event
            for event in cycle.cycle_trace
            if event["type"].startswith("cycle_compaction")
        ]
        serialized = json.dumps(compact_events, ensure_ascii=False)
        self.assertNotIn("OLD_BLOCK_0", serialized)
        self.assertNotIn("working summary", serialized)
        started = next(
            event
            for event in events
            if event["type"] == "cycle_compaction_started"
        )
        done = next(
            event
            for event in events
            if event["type"] == "cycle_compaction_done"
        )
        self.assertEqual(started["visibility"], "user")
        self.assertEqual(done["visibility"], "internal")
        self.assertIn("before_tokens", started["data"])
        self.assertIn("generation", done["data"])
        self.assertNotIn(
            "working summary",
            json.dumps(events, ensure_ascii=False),
        )

    async def test_trigger_accounts_for_runtime_state_and_tool_schemas(self):
        cycle = self._active_cycle(block_count=0)
        large_tools = [{
            "type": "function",
            "function": {
                "name": "large_tool",
                "description": "x" * 24_000,
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
            },
        }]
        messages_only = self.client._estimate_messages_tokens(
            cycle.messages_for_llm
        )
        self.client._call_llm_with_retries = AsyncMock()

        outcome = await self.client._compact_context_if_needed(
            active_cycle=cycle,
            state=self.state,
            session_id="session-1",
            progress_callback=None,
            request_tools=large_tools,
            request_iteration=1,
        )

        self.assertLess(
            messages_only,
            self.client._context_trigger_tokens(),
        )
        self.assertGreaterEqual(
            outcome.before_tokens,
            self.client._context_trigger_tokens(),
        )
        self.assertEqual(outcome.failure_reason, "no_safe_segment")
        self.client._call_llm_with_retries.assert_not_awaited()

    async def test_resumed_cycle_compacts_work_after_latest_user_reply(self):
        reply = dumps_json({
            "type": "user_reply",
            "reply": "confirmed",
        })
        messages = [
            {"role": "system", "content": "system"},
            {
                "role": "user",
                "content": dumps_json({
                    "type": "user_request",
                    "user_request": "complete the task",
                }),
            },
            {"role": "assistant", "content": "Should I continue?"},
            {"role": "user", "content": reply},
        ]
        messages.extend(
            {
                "role": "assistant",
                "content": f"POST_REPLY_BLOCK_{index}_" + "x" * 3_000,
            }
            for index in range(8)
        )
        cycle = ActiveAgentCycle(
            cycle_id="cycle-resumed",
            session_id="session-1",
            original_user_request="complete the task",
            messages_for_llm=messages,
            cycle_trace=[],
            original_user_message_index=1,
        )
        self.client._call_llm_with_retries = AsyncMock(
            return_value=valid_compaction_response()
        )

        outcome = await self._compact(cycle)

        self.assertTrue(outcome.changed)
        reply_index = next(
            index
            for index, message in enumerate(cycle.messages_for_llm)
            if message.get("content") == reply
        )
        memory_index = next(
            index
            for index, message in enumerate(cycle.messages_for_llm)
            if parse_cycle_working_memory_message(message) is not None
        )
        self.assertGreater(memory_index, reply_index)
        self.assertEqual(
            cycle.messages_for_llm[reply_index],
            {"role": "user", "content": reply},
        )
        self.assertNotIn(
            "POST_REPLY_BLOCK_0",
            json.dumps(cycle.messages_for_llm, ensure_ascii=False),
        )
        self.assertIn(
            "POST_REPLY_BLOCK_7",
            json.dumps(cycle.messages_for_llm, ensure_ascii=False),
        )
        self.client._call_llm_with_retries.assert_awaited()

    async def test_below_trigger_is_noop_without_progress(self):
        cycle = self._active_cycle(block_count=1, block_chars=100)
        events = []
        self.client._call_llm_with_retries = AsyncMock()

        outcome = await self._compact(cycle, events)

        self.assertFalse(outcome.changed)
        self.assertIsNone(outcome.failure_reason)
        self.assertEqual(events, [])
        self.client._call_llm_with_retries.assert_not_awaited()

    async def test_disabled_compaction_warns_or_stops_at_hard_limit(self):
        self.client.llm_config.enable_context_compaction = False
        safe_cycle = self._active_cycle()
        self.client._call_llm_with_retries = AsyncMock()

        safe_outcome = await self._compact(safe_cycle)

        self.assertFalse(safe_outcome.changed)
        self.assertEqual(
            safe_outcome.failure_reason,
            "compaction_disabled",
        )
        warning = next(
            event
            for event in safe_cycle.cycle_trace
            if event["type"] == "context_warning"
        )
        self.assertEqual(warning["compaction_status"], "disabled")
        self.client._call_llm_with_retries.assert_not_awaited()

        hard_cycle = self._active_cycle(
            block_count=10,
            block_chars=5_000,
        )
        with self.assertRaises(CycleContextLimitError):
            await self._compact(hard_cycle)

    def test_summary_budget_never_exceeds_small_output_limit(self):
        self.client.llm_config.max_tokens = 64

        self.assertEqual(self.client._cycle_summary_target_tokens(), 64)
        self.assertEqual(self.client._cycle_compactor_output_tokens(), 64)

    def test_structured_output_budget_is_separate_from_summary_target(self):
        self.client.llm_config.max_tokens = 4_096
        self.client.llm_config.context_window_tokens = 262_144
        self.client.llm_config.reserved_output_tokens = 8_192

        summary_target = self.client._cycle_summary_target_tokens()

        self.assertLess(summary_target, 1_536)
        self.assertEqual(summary_target, 512)
        self.assertEqual(
            self.client._cycle_compactor_output_tokens(),
            2_048,
        )

    async def test_truncated_output_is_repaired_with_same_output_budget(self):
        self.client.llm_config.max_tokens = 4_096
        self.client.llm_config.reserved_output_tokens = 4_096
        self.client.memory_config.cycle_compaction_max_passes = 1
        cycle = self._active_cycle()
        self.client._call_llm_with_retries = AsyncMock(side_effect=[
            {
                "content": "",
                self.client.LLM_RUNTIME_METADATA_KEY: {
                    "content_chars": 0,
                    "finish_reason": "length",
                    "prompt_tokens": 5_976,
                    "completion_tokens": 2_048,
                    "total_tokens": 8_024,
                },
            },
            valid_compaction_response(),
        ])

        outcome = await self._compact(cycle)

        self.assertTrue(outcome.changed)
        self.assertEqual(
            self.client._call_llm_with_retries.await_count,
            2,
        )
        first_call, repair_call = (
            self.client._call_llm_with_retries.await_args_list
        )
        self.assertEqual(
            first_call.kwargs["max_tokens_override"],
            2_048,
        )
        self.assertEqual(
            repair_call.kwargs["max_tokens_override"],
            2_048,
        )
        self.assertEqual(
            repair_call.kwargs["temperature_override"],
            0.0,
        )
        retry = next(
            event
            for event in cycle.cycle_trace
            if event["type"] == "cycle_compaction_retry"
        )
        self.assertEqual(retry["first_finish_reason"], "length")
        self.assertEqual(retry["reason"], "output_budget_exhausted")
        self.assertNotIn(
            "OLD_BLOCK_0",
            json.dumps(cycle.cycle_trace, ensure_ascii=False),
        )

    async def test_invalid_output_is_atomic_and_repeat_is_skipped(self):
        cycle = self._active_cycle()
        original = list(cycle.messages_for_llm)
        self.client._call_llm_with_retries = AsyncMock(
            return_value={"content": "not-json"}
        )

        first = await self._compact(cycle)
        second = await self._compact(cycle)

        self.assertFalse(first.changed)
        self.assertEqual(first.failure_reason, "invalid_compaction_output")
        self.assertEqual(cycle.messages_for_llm, original)
        self.assertIsNone(cycle.working_memory)
        self.assertEqual(cycle.compaction_failures, 1)
        self.assertEqual(
            second.failure_reason,
            "unchanged_context_after_failure",
        )
        self.assertEqual(
            self.client._call_llm_with_retries.await_count,
            2,
        )
        self.assertIn(
            "cycle_compaction_skipped",
            [event["type"] for event in cycle.cycle_trace],
        )
        failed = next(
            event
            for event in self.state.progress_events
            if event["type"] == "cycle_compaction_failed"
        )
        self.assertEqual(failed["visibility"], "user")
        self.assertIn("before_tokens", failed["data"])
        self.assertIn("generation", failed["data"])
        self.assertNotIn("segment_content_id", failed["data"])
        self.assertNotIn("not-json", json.dumps(failed, ensure_ascii=False))

    async def test_unchanged_selection_signature_suppresses_repeat_warning(self):
        cycle = self._active_cycle()
        decision, _ = self.client._select_cycle_segment_to_fit(
            active_cycle=cycle,
            messages=cycle.messages_for_llm,
            current_tokens=self.client._estimate_messages_tokens(
                cycle.messages_for_llm
            ),
            target_tokens=self.client._context_target_tokens(),
            summary_target_tokens=self.client._cycle_summary_target_tokens(),
            expected_compacted_tokens=(
                self.client._cycle_compactor_output_tokens()
            ),
            compactor_input_limit_tokens=(
                self.client._context_trigger_tokens()
            ),
        )
        cycle.compaction_failures = 1
        cycle.last_compaction_message_count = (
            len(cycle.messages_for_llm) - 1
        )
        cycle.last_compaction_failure_signature = (
            decision.retry_signature()
        )
        events = []
        self.client._call_llm_with_retries = AsyncMock()

        outcome = await self._compact(cycle, events)

        self.assertFalse(outcome.changed)
        self.assertEqual(
            outcome.failure_reason,
            "unchanged_context_after_failure",
        )
        self.assertEqual(events, [])
        self.client._call_llm_with_retries.assert_not_awaited()

    def test_exact_preflight_shrinks_selection_on_block_boundary(self):
        cycle = self._active_cycle()
        trigger_tokens = self.client._context_trigger_tokens()
        original_estimator = (
            self.client._estimate_cycle_compactor_input_tokens
        )
        inspected_message_counts = []

        def controlled_estimator(**kwargs):
            message_count = len(kwargs["selection"].messages)
            inspected_message_counts.append(message_count)
            if message_count > 2:
                return trigger_tokens + 1
            return original_estimator(**kwargs)

        self.client._estimate_cycle_compactor_input_tokens = Mock(
            side_effect=controlled_estimator
        )

        decision, compactor_input_tokens = (
            self.client._select_cycle_segment_to_fit(
                active_cycle=cycle,
                messages=cycle.messages_for_llm,
                current_tokens=self.client._estimate_messages_tokens(
                    cycle.messages_for_llm
                ),
                target_tokens=self.client._context_target_tokens(),
                summary_target_tokens=(
                    self.client._cycle_summary_target_tokens()
                ),
                expected_compacted_tokens=(
                    self.client._cycle_compactor_output_tokens()
                ),
                compactor_input_limit_tokens=trigger_tokens,
            )
        )

        self.assertIsNotNone(decision.selection)
        self.assertEqual(len(decision.selection.messages), 2)
        self.assertLessEqual(compactor_input_tokens, trigger_tokens)
        self.assertGreater(max(inspected_message_counts), 2)
        resize = next(
            event
            for event in cycle.cycle_trace
            if event["type"] == "cycle_compaction_selection_resized"
        )
        self.assertGreater(
            resize["compactor_input_tokens"],
            resize["input_limit_tokens"],
        )
        self.assertNotIn(
            "OLD_BLOCK_0",
            json.dumps(resize, ensure_ascii=False),
        )

    async def test_failed_exact_preflight_does_not_persist_segment(self):
        cycle = self._active_cycle()
        original = list(cycle.messages_for_llm)
        self.client._estimate_cycle_compactor_input_tokens = Mock(
            return_value=self.client._context_trigger_tokens() + 1
        )
        self.client.cycle_compaction_service.persist_segment = AsyncMock()
        self.client._call_llm_with_retries = AsyncMock()

        outcome = await self._compact(cycle)

        self.assertFalse(outcome.changed)
        self.assertEqual(outcome.failure_reason, "no_safe_segment")
        self.assertEqual(cycle.messages_for_llm, original)
        (
            self.client.cycle_compaction_service.persist_segment
            .assert_not_awaited()
        )
        self.client._call_llm_with_retries.assert_not_awaited()

    async def test_persistence_failure_never_calls_compactor(self):
        cycle = self._active_cycle()
        original = list(cycle.messages_for_llm)
        self.client.cycle_compaction_service.persist_segment = AsyncMock(
            side_effect=StorageError("disk unavailable")
        )
        self.client._call_llm_with_retries = AsyncMock()

        outcome = await self._compact(cycle)

        self.assertFalse(outcome.changed)
        self.assertEqual(
            outcome.failure_reason,
            "segment_persistence_failed",
        )
        self.assertEqual(cycle.messages_for_llm, original)
        self.client._call_llm_with_retries.assert_not_awaited()

    async def test_no_context_reduction_rejects_candidate(self):
        cycle = self._active_cycle()
        original = list(cycle.messages_for_llm)
        self.client._call_llm_with_retries = AsyncMock(
            return_value=valid_compaction_response("z" * 40_000)
        )

        outcome = await self._compact(cycle)

        self.assertFalse(outcome.changed)
        self.assertEqual(outcome.failure_reason, "no_context_reduction")
        self.assertEqual(cycle.messages_for_llm, original)
        self.assertIsNone(cycle.working_memory)

    async def test_hard_limit_turns_logical_failure_into_resumable_error(self):
        cycle = self._active_cycle(block_count=10, block_chars=5_000)
        self.client._call_llm_with_retries = AsyncMock(
            return_value={"content": "not-json"}
        )

        with self.assertRaises(CycleContextLimitError):
            await self._compact(cycle)

        self.assertIsNone(cycle.working_memory)
        self.assertGreaterEqual(
            self.client._estimate_messages_tokens(
                cycle.messages_for_llm
            ),
            self.client._context_usable_input_tokens(),
        )

    async def test_process_query_compacts_before_main_iteration(self):
        self.client.memory_config.cycle_compaction_max_passes = 1
        cycle = self._active_cycle(block_count=8, block_chars=3_000)
        cycle.status = "interrupted"
        cycle.interruption_reason = "temporary interruption"
        self.client.sessions["session-1"] = self.client._get_or_create_session(
            "session-1"
        )
        self.client.sessions["session-1"].pending_cycle = cycle
        self.client._call_llm_with_retries = AsyncMock(side_effect=[
            valid_compaction_response(),
            {
                "content": dumps_json({
                    "type": "agent_action",
                    "status": "done",
                    "action": "answer",
                    "agent_request": None,
                    "final_answer": "Finished after compaction",
                    "question_to_user": None,
                    "error_message": None,
                })
            },
        ])
        self.client._archive_agent_cycle = Mock()

        result = await self.client.process_query(
            "continue",
            session_id="session-1",
            progress_locale="en",
        )

        self.assertEqual(result.content, "Finished after compaction")
        self.assertEqual(result.iterations, 1)
        self.assertEqual(self.client._call_llm_with_retries.await_count, 2)
        compactor_call, main_call = (
            self.client._call_llm_with_retries.await_args_list
        )
        self.assertEqual(compactor_call.args[1], [])
        self.assertTrue(
            compactor_call.kwargs["context"].startswith(
                "Cycle compaction:"
            )
        )
        main_messages = main_call.args[0]
        memories = [
            parse_cycle_working_memory_message(message)
            for message in main_messages
            if parse_cycle_working_memory_message(message) is not None
        ]
        self.assertEqual(len(memories), 1)
        self.assertNotIn(
            "OLD_BLOCK_0",
            json.dumps(main_messages, ensure_ascii=False),
        )
        archived_cycle = (
            self.client._archive_agent_cycle.call_args.kwargs[
                "active_cycle"
            ]
        )
        self.assertIs(archived_cycle, cycle)
        self.assertEqual(archived_cycle.working_memory.generation, 1)
        self.assertTrue(
            archived_cycle.working_memory.archived_segment_refs
        )
        segment_content_id = (
            archived_cycle.working_memory.archived_segment_refs[0]
        )
        persisted = json.loads(
            await self.services.content_store.read_text(
                segment_content_id
            )
        )
        self.assertIn(
            "OLD_BLOCK_0",
            json.dumps(persisted, ensure_ascii=False),
        )
        evidence = self.client._build_final_evidence_pack(
            original_user_request=cycle.original_user_request,
            state=self.client.get_session_state("session-1"),
            cycle_trace=cycle.cycle_trace,
        )
        self.assertNotIn(
            "cycle_working_memory",
            json.dumps(evidence, ensure_ascii=False),
        )


if __name__ == "__main__":
    unittest.main()
