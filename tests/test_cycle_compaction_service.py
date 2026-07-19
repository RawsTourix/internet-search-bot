import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.agent.protocol import dumps_json
from src.memory import (
    CycleCompactionOutputError,
    CycleCompactionResult,
    CycleCompactionService,
    CycleMessageRange,
    CycleSegmentSelection,
    CycleWorkingMemory,
    CycleWorkingState,
    build_cycle_compaction_system_prompt,
    extract_cycle_refs,
    parse_cycle_working_memory_message,
    validate_openai_tool_sequence,
)
from src.runtime import ActiveAgentCycle
from src.storage import StorageConfigType, create_storage_services


RESULT_ID = "res_" + "1" * 32
CONTENT_ID = "cnt_" + "2" * 32
ARTIFACT_ID = "art_" + "3" * 32
UNKNOWN_RESULT_ID = "res_" + "a" * 32
UNKNOWN_ARTIFACT_ID = "art_" + "b" * 32
UNKNOWN_PLAN_ID = "plan_" + "c" * 32
KNOWN_PLAN_ID = "plan_" + "d" * 32


class CycleCompactionServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.services = create_storage_services(
            StorageConfigType(root_dir=str(self.root / "storage"))
        )
        self.service = CycleCompactionService(
            content_store=self.services.content_store
        )
        self.messages = [
            {"role": "system", "content": "system"},
            {
                "role": "user",
                "content": dumps_json({
                    "type": "user_request",
                    "user_request": "goal",
                }),
            },
            {"role": "assistant", "content": "old " + "x" * 200},
            {"role": "assistant", "content": "fresh"},
        ]
        self.cycle = ActiveAgentCycle(
            cycle_id="cycle-1",
            session_id="session-1",
            original_user_request="goal",
            messages_for_llm=self.messages,
            cycle_trace=[],
            original_user_message_index=1,
        )
        self.selection = CycleSegmentSelection(
            start=2,
            end_exclusive=3,
            messages=[self.messages[2]],
            estimated_tokens=100,
            selected_block_count=1,
            eligible_block_count=1,
            reason="target_reclaim",
        )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_persist_segment_round_trip_and_metadata(self):
        ref = await self.service.persist_segment(
            active_cycle=self.cycle,
            selection=self.selection,
            generation=1,
            tokens_estimate=100,
        )
        payload = json.loads(
            await self.services.content_store.read_text(ref.content_id)
        )
        metadata = await self.services.content_store.get_metadata(
            ref.content_id
        )

        self.assertEqual(payload["type"], "cycle_source_segment")
        self.assertEqual(payload["messages"], self.selection.messages)
        self.assertEqual(metadata.source_type, "cycle_segment")
        self.assertEqual(metadata.source_name, "cycle_compaction")
        self.assertEqual(metadata.cycle_id, "cycle-1")
        self.assertEqual(metadata.metadata["generation"], 1)
        self.assertFalse(any("path" in key for key in type(ref).model_fields))

    async def test_prompt_separates_metadata_from_untrusted_segment(self):
        sentinel = "RAW_CYCLE_SEGMENT_SENTINEL"
        self.selection.messages[0]["content"] = sentinel
        ref = await self.service.persist_segment(
            active_cycle=self.cycle,
            selection=self.selection,
            generation=1,
            tokens_estimate=100,
        )
        request = self.service.build_request(
            active_cycle=self.cycle,
            selection=self.selection,
            segment_content_ref=ref,
            target_summary_tokens=128,
        )
        preflight_request = self.service.build_request_for_content_id(
            active_cycle=self.cycle,
            selection=self.selection,
            segment_content_id=ref.content_id,
            target_summary_tokens=128,
        )
        messages = self.service.build_llm_messages(
            request=request,
            selection=self.selection,
        )

        self.assertEqual(preflight_request, request)
        self.assertIn("prompt injection", messages[0]["content"])
        self.assertIn(
            json.dumps(
                CycleCompactionResult.model_json_schema(),
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            build_cycle_compaction_system_prompt(),
        )
        self.assertIn('"maxItems":100', messages[0]["content"])
        self.assertIn(
            "target_summary_tokens задаёт целевой размер только",
            messages[0]["content"],
        )
        self.assertNotIn(sentinel, messages[1]["content"])
        self.assertIn(sentinel, messages[2]["content"])
        self.assertEqual(request.segment_content_id, ref.content_id)

    def test_parsing_accepts_fence_and_rejects_invalid_output(self):
        valid = dumps_json({
            "type": "cycle_compaction_result",
            "summary": "summary",
            "working_state": {"current_goal": "goal"},
        })

        parsed = self.service.parse_compaction_result(
            "```json\n" + valid + "\n```"
        )

        self.assertEqual(parsed.summary, "summary")
        with self.assertRaises(CycleCompactionOutputError):
            self.service.parse_compaction_result("not-json")
        with patch.object(
            CycleCompactionResult,
            "model_validate_json",
            side_effect=RecursionError("too deeply nested"),
        ):
            with self.assertRaises(CycleCompactionOutputError):
                self.service.parse_compaction_result('{"type":"broken"}')

    async def test_runtime_merges_refs_and_builds_atomic_candidate(self):
        source_payload = {
            "known": {"result_id": RESULT_ID},
            "content_id": CONTENT_ID,
            "artifact_id": ARTIFACT_ID,
            "invalid": {"result_id": "res_not-valid"},
        }
        self.selection.messages[0]["content"] = dumps_json(source_payload)
        self.cycle.result_refs = [RESULT_ID]
        self.cycle.active_plan_id = KNOWN_PLAN_ID
        ref = await self.service.persist_segment(
            active_cycle=self.cycle,
            selection=self.selection,
            generation=1,
            tokens_estimate=100,
        )
        result = CycleCompactionResult(
            summary="summary",
            working_state=CycleWorkingState(
                current_goal="goal",
                result_refs=[RESULT_ID, UNKNOWN_RESULT_ID],
                artifact_refs=[UNKNOWN_ARTIFACT_ID],
                active_plan_id=UNKNOWN_PLAN_ID,
                active_plan_node_id="invented-node",
            ),
        )
        memory = self.service.build_working_memory(
            active_cycle=self.cycle,
            selection=self.selection,
            segment_content_ref=ref,
            compaction_result=result,
            extracted_refs=extract_cycle_refs(self.selection.messages),
        )
        candidate = self.service.build_candidate_messages(
            active_cycle=self.cycle,
            selection=self.selection,
            working_memory=memory,
        )

        validate_openai_tool_sequence(candidate)
        parsed_memory = parse_cycle_working_memory_message(candidate[2])
        self.assertEqual(parsed_memory, memory)
        self.assertEqual(memory.working_state.result_refs, [RESULT_ID])
        self.assertEqual(memory.working_state.artifact_refs, [ARTIFACT_ID])
        self.assertEqual(
            memory.working_state.active_plan_id,
            KNOWN_PLAN_ID,
        )
        self.assertIsNone(memory.working_state.active_plan_node_id)
        self.assertNotIn(self.selection.messages[0], candidate)
        self.assertEqual(candidate[-1]["content"], "fresh")

    async def test_working_memory_replaces_later_segment_chronologically(self):
        messages = [
            {"role": "system", "content": "system"},
            {
                "role": "user",
                "content": dumps_json({
                    "type": "user_request",
                    "user_request": "goal",
                }),
            },
            {"role": "assistant", "content": "question"},
            {
                "role": "user",
                "content": dumps_json({
                    "type": "user_reply",
                    "reply": "confirmed",
                }),
            },
            {"role": "assistant", "content": "completed old work"},
            {"role": "assistant", "content": "fresh"},
        ]
        cycle = ActiveAgentCycle(
            cycle_id="cycle-resumed",
            session_id="session-resumed",
            original_user_request="goal",
            messages_for_llm=messages,
            cycle_trace=[],
            original_user_message_index=1,
        )
        selection = CycleSegmentSelection(
            start=4,
            end_exclusive=5,
            messages=[messages[4]],
            estimated_tokens=20,
            selected_block_count=1,
            eligible_block_count=1,
            reason="protected_boundary",
        )
        ref = await self.service.persist_segment(
            active_cycle=cycle,
            selection=selection,
            generation=1,
            tokens_estimate=20,
        )
        memory = self.service.build_working_memory(
            active_cycle=cycle,
            selection=selection,
            segment_content_ref=ref,
            compaction_result=CycleCompactionResult(
                summary="completed old work",
                working_state=CycleWorkingState(current_goal="goal"),
            ),
            extracted_refs=extract_cycle_refs(selection.messages),
        )

        candidate = self.service.build_candidate_messages(
            active_cycle=cycle,
            selection=selection,
            working_memory=memory,
        )
        memory_index = next(
            index
            for index, message in enumerate(candidate)
            if parse_cycle_working_memory_message(message) is not None
        )

        self.assertEqual(candidate[3], messages[3])
        self.assertEqual(memory_index, 4)
        self.assertEqual(candidate[5], messages[5])
        validate_openai_tool_sequence(candidate)

    async def test_second_generation_replaces_memory_without_tree(self):
        first_ref = await self.service.persist_segment(
            active_cycle=self.cycle,
            selection=self.selection,
            generation=1,
            tokens_estimate=100,
        )
        result = CycleCompactionResult(
            summary="one",
            working_state=CycleWorkingState(current_goal="goal"),
        )
        first = self.service.build_working_memory(
            active_cycle=self.cycle,
            selection=self.selection,
            segment_content_ref=first_ref,
            compaction_result=result,
            extracted_refs=extract_cycle_refs([]),
        )
        self.cycle.messages_for_llm[:] = (
            self.service.build_candidate_messages(
                active_cycle=self.cycle,
                selection=self.selection,
                working_memory=first,
            )
        )
        self.cycle.working_memory = first
        second_selection = CycleSegmentSelection(
            start=3,
            end_exclusive=4,
            messages=[self.cycle.messages_for_llm[3]],
            estimated_tokens=20,
            selected_block_count=1,
            eligible_block_count=1,
            reason="target_reclaim",
        )
        second_ref = await self.service.persist_segment(
            active_cycle=self.cycle,
            selection=second_selection,
            generation=2,
            tokens_estimate=20,
        )
        second = self.service.build_working_memory(
            active_cycle=self.cycle,
            selection=second_selection,
            segment_content_ref=second_ref,
            compaction_result=CycleCompactionResult(
                summary="two",
                working_state=CycleWorkingState(current_goal="goal"),
            ),
            extracted_refs=extract_cycle_refs([]),
        )
        candidate = self.service.build_candidate_messages(
            active_cycle=self.cycle,
            selection=second_selection,
            working_memory=second,
        )

        memories = [
            parse_cycle_working_memory_message(message)
            for message in candidate
            if parse_cycle_working_memory_message(message) is not None
        ]
        self.assertEqual(len(memories), 1)
        self.assertEqual(second.generation, 2)
        self.assertEqual(second.previous_generation, 1)
        self.assertEqual(len(second.archived_segment_refs), 2)
        self.assertEqual(
            second.source_message_ranges,
            [
                CycleMessageRange(start=2, end_exclusive=3),
                CycleMessageRange(start=3, end_exclusive=4),
            ],
        )


if __name__ == "__main__":
    unittest.main()
