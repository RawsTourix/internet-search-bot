import unittest

from src.agent.protocol import dumps_json
from src.memory import (
    CycleSegmentSelectionError,
    CycleSegmentSelector,
    validate_openai_tool_sequence,
)


def user_payload(payload_type, **values):
    return {
        "role": "user",
        "content": dumps_json({"type": payload_type, **values}),
    }


def assistant_tool(*call_ids):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": "tool", "arguments": "{}"},
            }
            for call_id in call_ids
        ],
    }


def tool_result(call_id, *, error=False):
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "content": dumps_json({
            "type": "tool_error" if error else "tool_result",
            "content": "x" * 100,
        }),
    }


class AtomicToolSequenceTests(unittest.TestCase):
    def setUp(self):
        self.prefix = [
            {"role": "system", "content": "system"},
            user_payload("user_request", user_request="goal"),
        ]

    def test_single_and_multiple_tool_calls_are_closed(self):
        for tail in (
            [assistant_tool("call-1"), tool_result("call-1")],
            [
                assistant_tool("call-1", "call-2"),
                tool_result("call-1"),
                tool_result("call-2"),
            ],
        ):
            with self.subTest(count=len(tail)):
                validate_openai_tool_sequence(self.prefix + tail)

    def test_incomplete_duplicate_or_orphan_tool_result_is_rejected(self):
        invalid_histories = (
            self.prefix + [
                assistant_tool("call-1", "call-2"),
                tool_result("call-1"),
            ],
            self.prefix + [
                assistant_tool("call-1", "call-2"),
                tool_result("call-1"),
                tool_result("call-1"),
            ],
            self.prefix + [tool_result("call-1")],
            self.prefix + [
                assistant_tool("call-1"),
                user_payload("user_reply", reply="interrupt"),
            ],
        )

        for messages in invalid_histories:
            with self.subTest(messages=messages):
                with self.assertRaises(CycleSegmentSelectionError):
                    validate_openai_tool_sequence(messages)


class CycleSegmentSelectorTests(unittest.TestCase):
    def setUp(self):
        self.selector = CycleSegmentSelector(
            lambda messages: max(1, len(str(messages)))
        )

    def _history(self):
        return [
            {"role": "system", "content": "system"},
            user_payload("session_dialog_memory", turns=[]),
            user_payload("user_request", user_request="goal"),
            {"role": "assistant", "content": "old action " + "a" * 100},
            user_payload("user_reply", reply="older addendum"),
            assistant_tool("call-1", "call-2"),
            tool_result("call-1"),
            tool_result("call-2"),
            user_payload("user_reply", reply="latest addendum"),
            {"role": "assistant", "content": "fresh action"},
        ]

    def test_selector_preserves_prefix_latest_user_and_recent_tail(self):
        messages = self._history()
        selection = self.selector.select(
            messages=messages,
            original_user_message_index=2,
            current_tokens=1_000,
            target_tokens=400,
            expected_summary_tokens=20,
            max_compactor_input_tokens=2_000,
            keep_recent_blocks=1,
        )

        self.assertIsNotNone(selection)
        self.assertGreaterEqual(selection.start, 3)
        self.assertLessEqual(selection.end_exclusive, 8)
        self.assertEqual(
            selection.messages,
            messages[selection.start:selection.end_exclusive],
        )
        selected_ids = [
            message.get("tool_call_id")
            for message in selection.messages
            if message.get("role") == "tool"
        ]
        if selected_ids:
            self.assertEqual(selected_ids, ["call-1", "call-2"])
        self.assertNotIn(messages[0], selection.messages)
        self.assertNotIn(messages[2], selection.messages)
        self.assertNotIn(messages[8], selection.messages)

    def test_open_group_and_everything_after_it_stay_visible(self):
        messages = self._history()[:4] + [
            assistant_tool("call-open", "call-missing"),
            tool_result("call-open"),
            user_payload("user_reply", reply="after unsafe group"),
        ]
        blocks = self.selector.build_blocks(
            messages=messages,
            original_user_message_index=2,
        )
        self.assertFalse(blocks[-2].closed)

        selection = self.selector.select(
            messages=messages,
            original_user_message_index=2,
            current_tokens=1_000,
            target_tokens=400,
            expected_summary_tokens=10,
            max_compactor_input_tokens=2_000,
            keep_recent_blocks=1,
        )

        if selection is not None:
            self.assertLessEqual(selection.end_exclusive, 4)

    def test_latest_error_block_and_oversized_atomic_block_are_protected(self):
        messages = self._history()
        messages[7] = tool_result("call-2", error=True)
        selection = self.selector.select(
            messages=messages,
            original_user_message_index=2,
            current_tokens=1_000,
            target_tokens=400,
            expected_summary_tokens=10,
            max_compactor_input_tokens=50,
            keep_recent_blocks=1,
        )

        self.assertIsNone(selection)

    def test_evaluate_explains_when_recent_blocks_leave_too_little_gain(self):
        messages = [
            {"role": "system", "content": "system"},
            user_payload("user_request", user_request="goal"),
            {"role": "assistant", "content": "small old action"},
            user_payload("user_reply", reply="older addendum"),
            {"role": "assistant", "content": "x" * 400},
            user_payload("user_reply", reply="latest addendum"),
            {"role": "assistant", "content": "fresh action"},
        ]

        decision = self.selector.evaluate(
            messages=messages,
            original_user_message_index=1,
            current_tokens=1_000,
            target_tokens=400,
            expected_summary_tokens=100,
            max_compactor_input_tokens=2_000,
            keep_recent_blocks=3,
        )

        self.assertIsNone(decision.selection)
        self.assertEqual(decision.reason, "insufficient_summary_gain")
        self.assertEqual(decision.boundary_reason, "protected_boundary")
        self.assertEqual(decision.block_count, 4)
        self.assertEqual(decision.protected_block_count, 3)
        self.assertEqual(decision.eligible_block_count, 1)
        self.assertEqual(decision.selected_block_count, 1)
        self.assertLessEqual(
            decision.selected_tokens,
            decision.expected_summary_tokens,
        )
        diagnostics = decision.safe_log_data()
        self.assertNotIn("messages", diagnostics)
        self.assertNotIn("content", diagnostics)

    def test_selector_compacts_actions_after_latest_user_anchor(self):
        messages = [
            {"role": "system", "content": "system"},
            user_payload("user_request", user_request="goal"),
            {"role": "assistant", "content": "short question"},
            user_payload("user_reply", reply="confirmed"),
            assistant_tool("call-after-reply"),
            tool_result("call-after-reply"),
            {
                "role": "assistant",
                "content": "completed follow-up " + "x" * 500,
            },
            {"role": "assistant", "content": "fresh tail"},
        ]

        blocks = self.selector.build_blocks(
            messages=messages,
            original_user_message_index=1,
        )
        latest_user_block = next(
            block
            for block in blocks
            if block.start == 3
        )
        tool_block = next(
            block
            for block in blocks
            if block.start == 4
        )

        self.assertEqual(latest_user_block.end_exclusive, 4)
        self.assertTrue(latest_user_block.contains_user_message)
        self.assertEqual(tool_block.end_exclusive, 6)
        self.assertTrue(tool_block.closed)

        decision = self.selector.evaluate(
            messages=messages,
            original_user_message_index=1,
            current_tokens=3_000,
            target_tokens=500,
            expected_summary_tokens=100,
            max_compactor_input_tokens=5_000,
            keep_recent_blocks=1,
        )

        self.assertIsNotNone(decision.selection)
        self.assertEqual(decision.selection.start, 4)
        self.assertNotIn(messages[3], decision.selection.messages)
        self.assertIn(messages[4], decision.selection.messages)
        self.assertIn(messages[5], decision.selection.messages)
        self.assertEqual(decision.boundary_reason, "protected_boundary")


if __name__ == "__main__":
    unittest.main()
