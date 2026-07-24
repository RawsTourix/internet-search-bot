import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.memory import TokenEstimateConfidence, TokenEstimateSource
from src.mcp.artifact_composite_budget import (
    ArtifactCompositeBudgetMixin,
    _SafetyMarginedResultTokenEstimator,
)
from src.mcp.artifact_composite_recovery import ArtifactCompositeRecoveryMixin


class _Estimator:
    def __init__(self, *, source, confidence, divisor=4):
        self.source = source
        self.confidence = confidence
        self.divisor = divisor

    def estimate_text(self, text):
        return max(1, len(text) // self.divisor)

    def estimate_messages(self, messages):
        return 100

    def estimate_request(self, *, messages, tools):
        return 200


class _BudgetBase:
    def __init__(self, *, request_estimator, fallback):
        self.request_token_estimator = request_estimator
        self.token_estimator = fallback


class _BudgetSubject(ArtifactCompositeBudgetMixin, _BudgetBase):
    pass


class _RecoveryBase:
    def __init__(self, visible):
        self.visible = visible
        self.super_calls = 0
        self.instructions = "base instructions"

    async def _call_registered_tool(self, public_tool_name, arguments):
        self.super_calls += 1
        return SimpleNamespace(content=[])

    def _prepare_structured_tool_result_representation(self, **kwargs):
        return dict(self.visible)


class _RecoverySubject(ArtifactCompositeRecoveryMixin, _RecoveryBase):
    pass


class ArtifactCompositeRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_high_confidence_model_estimator_replaces_raw_char_estimate(self):
        primary = _Estimator(
            source=TokenEstimateSource.MODEL_TOKENIZER,
            confidence=TokenEstimateConfidence.HIGH,
            divisor=4,
        )
        fallback = _Estimator(
            source=TokenEstimateSource.HEURISTIC,
            confidence=TokenEstimateConfidence.LOW,
            divisor=1,
        )
        subject = _BudgetSubject(
            request_estimator=primary,
            fallback=fallback,
        )
        self.assertIsInstance(
            subject.token_estimator,
            _SafetyMarginedResultTokenEstimator,
        )
        estimate = subject.token_estimator.estimate_text("x" * 18_000)
        self.assertGreater(estimate, 4_500)
        self.assertLess(estimate, 6_000)

    def test_low_confidence_estimator_keeps_conservative_fallback(self):
        primary = _Estimator(
            source=TokenEstimateSource.HEURISTIC,
            confidence=TokenEstimateConfidence.LOW,
            divisor=4,
        )
        fallback = _Estimator(
            source=TokenEstimateSource.HEURISTIC,
            confidence=TokenEstimateConfidence.LOW,
            divisor=1,
        )
        subject = _BudgetSubject(
            request_estimator=primary,
            fallback=fallback,
        )
        self.assertIs(subject.token_estimator, fallback)

    def test_recovery_instructions_are_added_to_system_instructions(self):
        subject = _RecoverySubject({})
        self.assertIn("split_artifact_batch", subject.instructions)
        self.assertIn("result_handling", subject.instructions)

    def test_stored_only_batch_recommends_smaller_exact_batches(self):
        artifact_ids = [f"art_{index:032x}" for index in range(10)]
        subject = _RecoverySubject({
            "representation": "stored_only",
            "needs_retrieval": True,
            "items": [
                {
                    "request_index": index,
                    "requested_artifact_id": artifact_id,
                    "status": "ok",
                }
                for index, artifact_id in enumerate(artifact_ids)
            ],
        })
        visible = subject._prepare_structured_tool_result_representation(
            effective_tool_name="artifact_read_text",
            tool_payload={},
            stored_result_ref=None,
            summary=None,
            decision=None,
            result_metadata={},
        )
        self.assertEqual(visible["recommended_action"], "split_artifact_batch")
        self.assertTrue(visible["do_not_repeat_same_batch"])
        flattened = [
            item
            for batch in visible["suggested_batches"]
            for item in batch
        ]
        self.assertEqual(flattened, artifact_ids)
        self.assertTrue(all(len(batch) <= 4 for batch in visible["suggested_batches"]))

    def test_attributed_summary_does_not_request_batch_split(self):
        subject = _RecoverySubject({
            "representation": "summarized",
            "needs_retrieval": True,
            "items": [],
        })
        visible = subject._prepare_structured_tool_result_representation(
            effective_tool_name="artifact_read_text",
            tool_payload={},
            stored_result_ref=None,
            summary=None,
            decision=None,
            result_metadata={},
        )
        self.assertNotIn("recommended_action", visible)

    async def test_identical_batch_is_rejected_after_stored_only_result(self):
        artifact_ids = [f"art_{index:032x}" for index in range(6)]
        subject = _RecoverySubject({
            "representation": "stored_only",
            "needs_retrieval": True,
            "items": [
                {
                    "request_index": index,
                    "requested_artifact_id": artifact_id,
                    "status": "ok",
                }
                for index, artifact_id in enumerate(artifact_ids)
            ],
        })
        context = SimpleNamespace(
            active_cycle=SimpleNamespace(
                blocked_artifact_batch_signatures=[],
                cycle_trace=[],
            )
        )
        with patch(
            "src.mcp.artifact_composite_recovery.get_manager_context",
            return_value=context,
        ):
            subject._prepare_structured_tool_result_representation(
                effective_tool_name="artifact_read_text",
                tool_payload={},
                stored_result_ref=None,
                summary=None,
                decision=None,
                result_metadata={},
            )
            result = await subject._call_registered_tool(
                "artifact_read_text",
                {"artifact_ids": artifact_ids},
            )
        payload = json.loads(result.content[0].text)
        self.assertEqual(payload["type"], "artifact_batch_repetition_rejected")
        self.assertEqual(result.execution_disposition, "rejected")
        self.assertEqual(subject.super_calls, 0)


if __name__ == "__main__":
    unittest.main()
