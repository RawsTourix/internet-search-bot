import json
import unittest

from src.memory import (
    ConservativeResultFidelityPolicy,
    ConservativeTokenEstimator,
    ResultCompactionSummary,
)


class ConservativeResultFidelityPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = ConservativeResultFidelityPolicy(
            token_estimator=ConservativeTokenEstimator(),
            substantial_reduction_ratio=0.5,
        )

    def test_substantially_reduced_record_collection_requires_original(self):
        raw = json.dumps({
            "payload": {
                "collection": [
                    {"alpha": "a" * 500, "value": 1},
                    {"alpha": "b" * 500, "value": 2},
                ],
            },
        })
        summary = ResultCompactionSummary(
            summary="Two records.",
            needs_original_content=False,
        )

        hardened = self.policy.apply(
            raw_result=raw,
            summary=summary,
        )

        self.assertTrue(hardened.needs_original_content)
        self.assertIn(
            self.policy.DEFAULT_LIMITATION,
            hardened.limitations,
        )

    def test_single_record_does_not_force_retrieval(self):
        raw = json.dumps({
            "payload": {
                "collection": [{"alpha": "a" * 500}],
            },
        })
        summary = ResultCompactionSummary(summary="One record.")

        unchanged = self.policy.apply(
            raw_result=raw,
            summary=summary,
        )

        self.assertEqual(unchanged, summary)

    def test_unstructured_or_invalid_payload_is_not_reclassified(self):
        summary = ResultCompactionSummary(summary="Text result.")

        for raw in ("plain text", json.dumps(["a", "b"])):
            with self.subTest(raw=raw):
                self.assertEqual(
                    self.policy.apply(
                        raw_result=raw,
                        summary=summary,
                    ),
                    summary,
                )


if __name__ == "__main__":
    unittest.main()
