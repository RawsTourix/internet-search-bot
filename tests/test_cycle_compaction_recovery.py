import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from src.agent.protocol import dumps_json
from src.core.errors import LLMTransportError
from src.memory import (
    CycleWorkingMemory,
    CycleWorkingState,
    MemoryConfigType,
    parse_cycle_working_memory_message,
)
from src.mcp.mcp_client import (
    LLMConfigType,
    MCPClient,
    SessionMemory,
    SessionState,
)
from src.runtime import ActiveAgentCycle
from src.storage import StorageConfigType, create_storage_services

from tests.test_cycle_compaction_integration import (
    valid_compaction_response,
)


class CycleCompactionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        services = create_storage_services(
            StorageConfigType(root_dir=str(self.root / "storage"))
        )
        self.client = MCPClient(
            LLMConfigType(
                api_url="https://example.invalid/v1/chat/completions",
                context_window_tokens=20_000,
                reserved_output_tokens=1_000,
                max_tokens=1_000,
                context_safety_ratio=0.60,
                context_compaction_target_ratio=0.35,
            ),
            storage_services=services,
            memory_config=MemoryConfigType(
                cycle_compaction_keep_recent_blocks=1,
            ),
        )

    async def asyncTearDown(self):
        await self.client.http_client.aclose()
        self.temporary.cleanup()

    def _large_cycle(self):
        messages = [
            {"role": "system", "content": "system"},
            {
                "role": "user",
                "content": dumps_json({
                    "type": "user_request",
                    "user_request": "goal",
                }),
            },
        ]
        messages.extend(
            {
                "role": "assistant",
                "content": f"block-{index}-" + "x" * 3_000,
            }
            for index in range(8)
        )
        return ActiveAgentCycle(
            cycle_id="cycle-recovery",
            session_id="session-recovery",
            original_user_request="goal",
            messages_for_llm=messages,
            cycle_trace=[],
            original_user_message_index=1,
        )

    async def test_transport_failure_preserves_context_and_allows_retry(self):
        cycle = self._large_cycle()
        original = list(cycle.messages_for_llm)
        state = SessionState(progress_locale="en")
        self.client._call_llm_with_retries = AsyncMock(
            side_effect=LLMTransportError("network down")
        )

        with self.assertRaises(LLMTransportError):
            await self.client._compact_context_if_needed(
                active_cycle=cycle,
                state=state,
                session_id=cycle.session_id,
                progress_callback=None,
            )

        self.assertEqual(cycle.messages_for_llm, original)
        self.assertIsNone(cycle.working_memory)
        self.assertEqual(cycle.compaction_failures, 0)

        self.client._call_llm_with_retries = AsyncMock(
            return_value=valid_compaction_response()
        )
        outcome = await self.client._compact_context_if_needed(
            active_cycle=cycle,
            state=state,
            session_id=cycle.session_id,
            progress_callback=None,
        )

        self.assertTrue(outcome.changed)

    async def test_cancelled_error_is_not_suppressed(self):
        cycle = self._large_cycle()
        original = list(cycle.messages_for_llm)
        state = SessionState(progress_locale="en")
        self.client._call_llm_with_retries = AsyncMock(
            side_effect=asyncio.CancelledError()
        )

        with self.assertRaises(asyncio.CancelledError):
            await self.client._compact_context_if_needed(
                active_cycle=cycle,
                state=state,
                session_id=cycle.session_id,
                progress_callback=None,
            )

        self.assertEqual(cycle.messages_for_llm, original)
        self.assertIsNone(cycle.working_memory)

    async def test_process_query_preserves_hard_limit_cycle_for_resume(self):
        cycle = self._large_cycle()
        for message in cycle.messages_for_llm[2:]:
            message["content"] += "y" * 3_000
        cycle.status = "interrupted"
        cycle.interruption_reason = "previous interruption"
        self.client.sessions[cycle.session_id] = SessionMemory(
            pending_cycle=cycle
        )
        self.client._call_llm_with_retries = AsyncMock(
            return_value={"content": "not-json"}
        )
        self.client._archive_agent_cycle = Mock()

        result = await self.client.process_query(
            "resume",
            session_id=cycle.session_id,
            progress_locale="en",
        )

        self.assertEqual(result.status.value, "error")
        self.assertEqual(
            result.error_kind,
            "context_limit_interruption",
        )
        self.assertTrue(result.can_resume)
        self.assertIs(
            self.client.sessions[cycle.session_id].pending_cycle,
            cycle,
        )
        self.assertIsNone(cycle.working_memory)
        self.assertIn(
            "block-0-",
            cycle.messages_for_llm[2]["content"],
        )
        self.assertTrue(
            any(
                event["type"] == "cycle_compaction_failed"
                for event in result.progress_events
            )
        )

    async def test_process_query_transport_interruption_can_retry_compaction(self):
        cycle = self._large_cycle()
        cycle.status = "interrupted"
        cycle.interruption_reason = "previous interruption"
        self.client.sessions[cycle.session_id] = SessionMemory(
            pending_cycle=cycle
        )
        self.client._call_llm_with_retries = AsyncMock(
            side_effect=LLMTransportError("network down")
        )
        self.client._archive_agent_cycle = Mock()

        interrupted = await self.client.process_query(
            "first resume",
            session_id=cycle.session_id,
            progress_locale="en",
        )

        self.assertTrue(interrupted.can_resume)
        self.assertEqual(
            interrupted.error_kind,
            "infrastructure_interruption",
        )
        self.assertIs(
            self.client.sessions[cycle.session_id].pending_cycle,
            cycle,
        )
        self.assertIsNone(cycle.working_memory)
        self.assertIn(
            "block-0-",
            cycle.messages_for_llm[2]["content"],
        )

        self.client._call_llm_with_retries = AsyncMock(side_effect=[
            valid_compaction_response(),
            {
                "content": dumps_json({
                    "type": "agent_action",
                    "status": "done",
                    "action": "answer",
                    "agent_request": None,
                    "final_answer": "Recovered",
                    "question_to_user": None,
                    "error_message": None,
                })
            },
        ])

        recovered = await self.client.process_query(
            "second resume",
            session_id=cycle.session_id,
            progress_locale="en",
        )

        self.assertEqual(recovered.content, "Recovered")
        self.assertEqual(recovered.iterations, 1)
        self.assertEqual(cycle.working_memory.generation, 1)
        self.assertIsNone(
            self.client.sessions[cycle.session_id].pending_cycle
        )

    async def test_waiting_cycle_resumes_same_object_and_generation(self):
        memory = CycleWorkingMemory(
            generation=2,
            summary="previous work",
            working_state=CycleWorkingState(current_goal="goal"),
            archived_segment_count=2,
        )
        messages = [
            {"role": "system", "content": "system"},
            {
                "role": "user",
                "content": dumps_json({
                    "type": "user_request",
                    "user_request": "goal",
                }),
            },
            {"role": "user", "content": memory.model_dump_json()},
            {
                "role": "assistant",
                "content": dumps_json({
                    "type": "agent_action",
                    "status": "waiting_user",
                    "action": "ask_user",
                    "agent_request": None,
                    "final_answer": None,
                    "question_to_user": "Confirm?",
                    "error_message": None,
                }),
            },
        ]
        cycle = ActiveAgentCycle(
            cycle_id="cycle-waiting",
            session_id="session-waiting",
            original_user_request="goal",
            messages_for_llm=messages,
            cycle_trace=[],
            original_user_message_index=1,
            working_memory=memory,
            status="waiting_user",
            waiting_question="Confirm?",
        )
        self.client.sessions["session-waiting"] = SessionMemory(
            pending_cycle=cycle
        )
        self.client._call_llm_with_retries = AsyncMock(return_value={
            "content": dumps_json({
                "type": "agent_action",
                "status": "done",
                "action": "answer",
                "agent_request": None,
                "final_answer": "Done",
                "question_to_user": None,
                "error_message": None,
            })
        })
        self.client._archive_agent_cycle = Mock()

        result = await self.client.process_query(
            "Yes",
            session_id="session-waiting",
            progress_locale="en",
        )

        self.assertEqual(result.content, "Done")
        archived_cycle = (
            self.client._archive_agent_cycle.call_args.kwargs[
                "active_cycle"
            ]
        )
        self.assertIs(archived_cycle, cycle)
        self.assertEqual(archived_cycle.cycle_id, "cycle-waiting")
        self.assertEqual(archived_cycle.working_memory.generation, 2)
        self.assertIsNone(
            self.client.sessions["session-waiting"].pending_cycle
        )
        memories = [
            parse_cycle_working_memory_message(message)
            for message in archived_cycle.messages_for_llm
            if parse_cycle_working_memory_message(message) is not None
        ]
        self.assertEqual(len(memories), 1)
        self.assertIn(
            "user_reply_during_waiting_user",
            archived_cycle.messages_for_llm[-2]["content"],
        )

    def test_legacy_snapshot_migration_inserts_visible_working_memory(self):
        legacy = SimpleNamespace(
            cycle_id="cycle-legacy",
            original_user_request="goal",
            messages_for_llm=[
                {"role": "system", "content": "system"},
                {
                    "role": "user",
                    "content": dumps_json({
                        "type": "user_request",
                        "user_request": "goal",
                    }),
                },
                {"role": "assistant", "content": "fresh tail"},
            ],
            cycle_trace=[],
            working_summary="legacy summary",
            working_state={"current_goal": "goal"},
        )

        migrated = self.client._coerce_active_cycle(
            legacy,
            session_id="session-legacy",
        )

        memories = [
            parse_cycle_working_memory_message(message)
            for message in migrated.messages_for_llm
            if parse_cycle_working_memory_message(message) is not None
        ]
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0].summary, "legacy summary")
        self.assertEqual(migrated.working_memory, memories[0])
        self.assertEqual(
            migrated.messages_for_llm[-1]["content"],
            "fresh tail",
        )


if __name__ == "__main__":
    unittest.main()
