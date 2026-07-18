import json
import unittest

from src.memory import (
    ResultContextBudgetPolicy,
    ResultHandling,
    estimate_untrusted_result_tokens,
)


class UntrustedResultTokenEstimatorTests(unittest.TestCase):
    def test_estimator_is_conservative_for_representative_payloads(self):
        samples = {
            "cyrillic": "Результат поиска и несколько фактов",
            "ascii": "plain ASCII result with identifiers 12345",
            "cjk": "検索結果東京天气预报",
            "emoji": "🔎🧩✅⚠️🚀",
            "minified_json": json.dumps(
                {"items": [{"id": index, "ok": True} for index in range(20)]},
                separators=(",", ":"),
            ),
            "escaped": "\\\\\\\"quoted\\\"\\\\path" * 20,
        }

        for name, text in samples.items():
            with self.subTest(name=name):
                expected = max(
                    1,
                    len(text),
                    (len(text.encode("utf-8")) + 1) // 2,
                )
                self.assertEqual(
                    estimate_untrusted_result_tokens(text),
                    expected,
                )

    def test_empty_result_still_uses_one_token(self):
        self.assertEqual(estimate_untrusted_result_tokens(""), 1)

    def test_precomputed_utf8_size_avoids_changing_estimate(self):
        text = "Результат 🔎"

        self.assertEqual(
            estimate_untrusted_result_tokens(
                text,
                utf8_size_bytes=len(text.encode("utf-8")),
            ),
            estimate_untrusted_result_tokens(text),
        )


class ResultContextBudgetPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = ResultContextBudgetPolicy(
            context_window_tokens=10_000,
            reserved_output_tokens=1_000,
            max_output_tokens=500,
            context_safety_ratio=0.8,
            context_compaction_target_ratio=0.5,
            inline_result_max_input_ratio=0.1,
            single_pass_summary_max_input_ratio=0.6,
            result_summary_target_tokens=128,
            result_compaction_max_output_tokens=400,
            max_in_memory_content_bytes=10_000,
        )

    def decide(
        self,
        *,
        handling=ResultHandling.AUTO,
        current=1_000,
        result=100,
        size_bytes=100,
        overhead=100,
        enabled=True,
    ):
        return self.policy.decide(
            handling=handling,
            current_context_tokens=current,
            result_tokens=result,
            result_size_bytes=size_bytes,
            summary_request_overhead_tokens=overhead,
            enable_result_compaction=enabled,
        )

    def test_relative_budget_formulas(self):
        self.assertEqual(self.policy.usable_input_tokens, 9_000)
        self.assertEqual(self.policy.trigger_tokens, 7_200)
        self.assertEqual(self.policy.target_tokens, 4_500)
        self.assertEqual(self.policy.inline_limit_tokens, 900)
        self.assertEqual(self.policy.single_pass_limit_tokens, 5_400)
        self.assertEqual(self.policy.summary_target_tokens, 128)
        self.assertEqual(self.policy.compactor_output_tokens, 400)

    def test_output_budgets_do_not_grow_with_context_window(self):
        large_context_policy = ResultContextBudgetPolicy(
            context_window_tokens=262_144,
            reserved_output_tokens=8_192,
            max_output_tokens=4_096,
            context_safety_ratio=0.8,
            context_compaction_target_ratio=0.5,
            inline_result_max_input_ratio=0.1,
            single_pass_summary_max_input_ratio=0.6,
            result_summary_target_tokens=128,
            result_compaction_max_output_tokens=400,
            max_in_memory_content_bytes=10_000,
        )

        self.assertEqual(large_context_policy.summary_target_tokens, 128)
        self.assertEqual(large_context_policy.compactor_output_tokens, 400)

    def test_small_auto_and_prefer_inline_are_inline(self):
        auto = self.decide()
        preferred = self.decide(handling=ResultHandling.PREFER_INLINE)

        self.assertEqual(auto.representation, "inline")
        self.assertFalse(auto.runtime_override)
        self.assertEqual(preferred.representation, "inline")
        self.assertFalse(preferred.runtime_override)

    def test_large_auto_is_summarized(self):
        decision = self.decide(result=1_000, size_bytes=2_000)

        self.assertEqual(decision.representation, "summarize")

    def test_prefer_inline_cannot_cross_context_trigger(self):
        decision = self.decide(
            handling=ResultHandling.PREFER_INLINE,
            current=7_000,
            result=200,
            size_bytes=400,
        )

        self.assertEqual(decision.representation, "summarize")
        self.assertTrue(decision.runtime_override)

    def test_compact_forces_summary_even_for_small_result(self):
        decision = self.decide(handling=ResultHandling.COMPACT)

        self.assertEqual(decision.representation, "summarize")

    def test_store_only_never_requests_summary(self):
        decision = self.decide(handling=ResultHandling.STORE_ONLY)

        self.assertEqual(decision.representation, "store_only")

    def test_single_pass_and_technical_limits_produce_oversized(self):
        single_pass = self.decide(
            result=5_301,
            size_bytes=9_000,
            overhead=100,
        )
        technical = self.decide(
            result=1_000,
            size_bytes=10_001,
        )

        self.assertEqual(single_pass.representation, "oversized")
        self.assertEqual(
            single_pass.reason,
            "single_pass_summary_limit_exceeded",
        )
        self.assertEqual(technical.representation, "oversized")
        self.assertEqual(
            technical.reason,
            "technical_memory_limit_exceeded",
        )

    def test_disabled_compaction_keeps_safe_inline_and_stores_unsafe(self):
        safe = self.decide(enabled=False)
        unsafe = self.decide(
            result=1_000,
            size_bytes=2_000,
            enabled=False,
        )
        compact = self.decide(
            handling=ResultHandling.COMPACT,
            enabled=False,
        )

        self.assertEqual(safe.representation, "inline")
        self.assertEqual(unsafe.representation, "store_only")
        self.assertEqual(compact.representation, "store_only")

    def test_inline_limit_boundaries(self):
        self.assertEqual(
            self.decide(result=899, size_bytes=899).representation,
            "inline",
        )
        self.assertEqual(
            self.decide(result=900, size_bytes=900).representation,
            "inline",
        )
        self.assertEqual(
            self.decide(result=901, size_bytes=901).representation,
            "summarize",
        )

    def test_context_trigger_uses_strict_candidate_comparison(self):
        # candidate = current + result + 64
        self.assertEqual(
            self.decide(current=7_035, result=100).representation,
            "inline",
        )
        self.assertEqual(
            self.decide(current=7_036, result=100).representation,
            "summarize",
        )
        self.assertEqual(
            self.decide(current=7_037, result=100).representation,
            "summarize",
        )

    def test_single_pass_limit_boundaries(self):
        for result, expected in (
            (5_299, "summarize"),
            (5_300, "summarize"),
            (5_301, "oversized"),
        ):
            with self.subTest(result=result):
                self.assertEqual(
                    self.decide(
                        result=result,
                        size_bytes=9_000,
                        overhead=100,
                    ).representation,
                    expected,
                )


if __name__ == "__main__":
    unittest.main()
