import unittest

from pydantic import ValidationError

from src.memory import (
    CycleMessageRange,
    CycleWorkingMemory,
    CycleWorkingState,
)
from src.runtime import ActiveAgentCycle, AgentCycleSnapshot


RESULT_ID = "res_" + "1" * 32
ARTIFACT_ID = "art_" + "2" * 32
CONTENT_ID = "cnt_" + "3" * 32


class CycleModelTests(unittest.TestCase):
    def test_working_state_normalizes_lists_and_refs(self):
        state = CycleWorkingState(
            current_goal="  continue task  ",
            completed_actions=[" done ", "", "done", "next"],
            result_refs=[RESULT_ID, RESULT_ID],
            artifact_refs=[ARTIFACT_ID, ARTIFACT_ID],
        )

        self.assertEqual(state.current_goal, "continue task")
        self.assertEqual(state.completed_actions, ["done", "next"])
        self.assertEqual(state.result_refs, [RESULT_ID])
        self.assertEqual(state.artifact_refs, [ARTIFACT_ID])

    def test_invalid_state_and_extra_fields_are_rejected(self):
        with self.assertRaises(ValidationError):
            CycleWorkingState(current_goal=" ")
        with self.assertRaises(ValidationError):
            CycleWorkingState(current_goal="goal", unknown=True)
        with self.assertRaises(ValidationError):
            CycleWorkingState(
                current_goal="goal",
                result_refs=["res_not-valid"],
            )

    def test_memory_validates_generation_summary_and_archive(self):
        state = CycleWorkingState(current_goal="goal")

        for kwargs in (
            {"generation": 0, "summary": "summary"},
            {"generation": 1, "summary": " "},
            {
                "generation": 2,
                "summary": "summary",
                "previous_generation": 2,
            },
            {
                "generation": 1,
                "summary": "summary",
                "archived_segment_refs": [CONTENT_ID],
                "archived_segment_count": 0,
            },
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValidationError):
                    CycleWorkingMemory(
                        working_state=state,
                        archived_segment_count=kwargs.pop(
                            "archived_segment_count",
                            1,
                        ),
                        **kwargs,
                    )

    def test_message_range_and_round_trip(self):
        with self.assertRaises(ValidationError):
            CycleMessageRange(start=2, end_exclusive=2)

        memory = CycleWorkingMemory(
            generation=2,
            previous_generation=1,
            summary=" summary ",
            working_state=CycleWorkingState(current_goal="goal"),
            source_message_ranges=[
                CycleMessageRange(start=2, end_exclusive=5)
            ],
            archived_segment_refs=[CONTENT_ID, CONTENT_ID],
            archived_segment_count=2,
        )
        restored = CycleWorkingMemory.model_validate_json(
            memory.model_dump_json()
        )

        self.assertEqual(restored, memory)
        self.assertEqual(restored.summary, "summary")
        self.assertEqual(restored.archived_segment_refs, [CONTENT_ID])
        self.assertFalse(
            any("path" in name for name in type(memory).model_fields)
        )

    def test_mutable_defaults_are_not_shared(self):
        first = CycleWorkingState(current_goal="one")
        second = CycleWorkingState(current_goal="two")

        first.completed_actions.append("done")

        self.assertEqual(second.completed_actions, [])

    def test_active_cycle_alias_is_one_model(self):
        cycle = ActiveAgentCycle(
            cycle_id="cycle-1",
            session_id="session-1",
            original_user_request="request",
            messages_for_llm=[],
            cycle_trace=[],
            original_user_message_index=0,
        )

        self.assertIs(AgentCycleSnapshot, ActiveAgentCycle)
        self.assertIsInstance(cycle, AgentCycleSnapshot)


if __name__ == "__main__":
    unittest.main()
