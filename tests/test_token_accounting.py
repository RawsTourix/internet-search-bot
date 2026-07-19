import json
import math
import unittest
from unittest.mock import patch

from src.memory import (
    ConservativeTokenEstimator,
    HeuristicRequestTokenEstimator,
    TiktokenRequestTokenEstimator,
    TokenAccountingService,
    TokenEstimateConfidence,
    TokenEstimateSource,
    create_request_token_estimator,
)


class DeterministicRequestEstimator:
    source = TokenEstimateSource.MODEL_TOKENIZER
    confidence = TokenEstimateConfidence.HIGH

    @staticmethod
    def _count(value) -> int:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return max(1, (len(serialized) + 1) // 2)

    def estimate_text(self, text: str) -> int:
        return max(1, (len(text) + 1) // 2)

    def estimate_messages(self, messages) -> int:
        return self._count(messages)

    def estimate_request(self, *, messages, tools) -> int:
        return self._count({
            "messages": messages,
            "tools": tools,
        })


class FakeEncoding:
    def __init__(self, name="fake_encoding"):
        self.name = name
        self.calls = []

    def encode(self, text, **kwargs):
        self.calls.append((text, kwargs))
        return list(range(max(1, len(text) // 3)))


class RequestEstimatorTests(unittest.TestCase):
    def test_request_fallback_is_less_aggressive_than_raw_estimator(self):
        text = "x" * 20_000

        raw = ConservativeTokenEstimator().estimate_text(text)
        request = HeuristicRequestTokenEstimator().estimate_text(text)

        self.assertEqual(raw, 20_000)
        self.assertEqual(request, 10_000)

    def test_request_fallback_accounts_for_utf8_density(self):
        text = "данные" * 1_000
        estimator = HeuristicRequestTokenEstimator()

        estimate = estimator.estimate_text(text)

        self.assertGreaterEqual(
            estimate,
            (len(text.encode("utf-8")) + 3) // 4,
        )

    def test_tiktoken_adapter_counts_without_special_token_failures(self):
        encoding = FakeEncoding()
        estimator = TiktokenRequestTokenEstimator(
            encoding,
            encoding_name=encoding.name,
            protocol_overhead_tokens=0,
        )

        result = estimator.estimate_text("<|custom_special|>")

        self.assertGreater(result, 0)
        self.assertEqual(
            encoding.calls[0][1]["disallowed_special"],
            (),
        )

    def test_configured_gpt_oss_encoding_is_available(self):
        with self.assertNoLogs(
            "src.memory.token_estimation",
            level="WARNING",
        ):
            estimator = TiktokenRequestTokenEstimator.for_model(
                "openai/gpt-oss-120b",
                encoding_name="o200k_harmony",
            )

        self.assertEqual(estimator.encoding_name, "o200k_harmony")
        self.assertEqual(estimator.selection_source, "explicit")
        self.assertGreater(estimator.estimate_text("test request"), 0)

    def test_invalid_explicit_encoding_falls_back_to_model_mapping(self):
        mapped_encoding = FakeEncoding("model_encoding")
        with (
            patch(
                "tiktoken.get_encoding",
                side_effect=KeyError("unknown encoding"),
            ),
            patch(
                "tiktoken.encoding_for_model",
                side_effect=[
                    KeyError("provider-qualified model is unknown"),
                    mapped_encoding,
                ],
            ) as encoding_for_model,
            self.assertLogs(
                "src.memory.token_estimation",
                level="WARNING",
            ) as logs,
        ):
            estimator = create_request_token_estimator(
                model="provider/model",
                encoding_name="missing_encoding",
            )

        self.assertIsInstance(
            estimator,
            TiktokenRequestTokenEstimator,
        )
        self.assertEqual(estimator.encoding_name, "model_encoding")
        self.assertEqual(
            estimator.selection_source,
            "model_mapping_after_explicit_failure",
        )
        self.assertEqual(
            estimator.requested_encoding,
            "missing_encoding",
        )
        self.assertEqual(estimator.model_mapping_candidate, "model")
        self.assertEqual(
            [call.args[0] for call in encoding_for_model.call_args_list],
            ["provider/model", "model"],
        )
        self.assertIn(
            "trying model mapping",
            "\n".join(logs.output),
        )

    def test_valid_explicit_encoding_wins_over_different_model_mapping(self):
        explicit_encoding = FakeEncoding("explicit_encoding")
        mapped_encoding = FakeEncoding("model_encoding")
        with (
            patch(
                "tiktoken.get_encoding",
                return_value=explicit_encoding,
            ),
            patch(
                "tiktoken.encoding_for_model",
                return_value=mapped_encoding,
            ),
            self.assertLogs(
                "src.memory.token_estimation",
                level="WARNING",
            ) as logs,
        ):
            estimator = TiktokenRequestTokenEstimator.for_model(
                "provider/model",
                encoding_name="explicit_encoding",
            )

        self.assertIs(estimator.encoding, explicit_encoding)
        self.assertEqual(estimator.selection_source, "explicit")
        self.assertIn(
            "keeping explicit override",
            "\n".join(logs.output),
        )

    def test_invalid_explicit_and_unknown_model_use_request_heuristic(self):
        with (
            patch(
                "tiktoken.get_encoding",
                side_effect=KeyError("unknown encoding"),
            ),
            patch(
                "tiktoken.encoding_for_model",
                side_effect=KeyError("unknown model"),
            ),
            self.assertLogs(
                "src.memory.token_estimation",
                level="WARNING",
            ) as logs,
        ):
            estimator = create_request_token_estimator(
                model="unknown-provider/unknown-model",
                encoding_name="missing_encoding",
            )

        self.assertIsInstance(
            estimator,
            HeuristicRequestTokenEstimator,
        )
        combined_logs = "\n".join(logs.output)
        self.assertIn("trying model mapping", combined_logs)
        self.assertIn("selection_source=heuristic", combined_logs)

    def test_missing_explicit_encoding_uses_model_mapping(self):
        mapped_encoding = FakeEncoding("model_encoding")
        with patch(
            "tiktoken.encoding_for_model",
            return_value=mapped_encoding,
        ):
            estimator = create_request_token_estimator(
                model="provider/model",
            )

        self.assertIsInstance(
            estimator,
            TiktokenRequestTokenEstimator,
        )
        self.assertEqual(estimator.selection_source, "model_mapping")
        self.assertIsNone(estimator.requested_encoding)
        self.assertEqual(
            estimator.model_mapping_candidate,
            "provider/model",
        )

    def test_unknown_model_uses_request_heuristic(self):
        estimator = create_request_token_estimator(
            model="unknown-provider/unknown-model",
        )

        self.assertIsInstance(
            estimator,
            HeuristicRequestTokenEstimator,
        )


class TokenAccountingServiceTests(unittest.TestCase):
    def setUp(self):
        self.estimator = DeterministicRequestEstimator()
        self.service = TokenAccountingService(
            model="provider/model",
            request_estimator=self.estimator,
        )
        self.messages = [
            {"role": "system", "content": "system instructions"},
            {"role": "user", "content": "do the work"},
        ]
        self.tools = [{
            "type": "function",
            "function": {
                "name": "search",
                "parameters": {"type": "object"},
            },
        }]

    def test_exact_provider_usage_replaces_local_request_estimate(self):
        snapshot = self.service.observe_prompt_usage(
            messages=self.messages,
            tools=self.tools,
            prompt_tokens=123,
        )

        accounting = self.service.estimate_request(
            messages=self.messages,
            tools=self.tools,
            usage_snapshot=snapshot,
        )

        self.assertEqual(accounting.total_tokens, 123)
        self.assertTrue(accounting.used_usage_snapshot)
        self.assertEqual(accounting.source, "model_tokenizer")
        self.assertEqual(accounting.confidence, "high")

    def test_changed_request_uses_actual_baseline_plus_estimated_growth(self):
        snapshot = self.service.observe_prompt_usage(
            messages=self.messages,
            tools=self.tools,
            prompt_tokens=123,
        )
        grown_messages = [
            *self.messages,
            {"role": "assistant", "content": "new evidence" * 20},
        ]
        grown_raw = self.estimator.estimate_request(
            messages=grown_messages,
            tools=self.tools,
        )
        expected = (
            123
            + grown_raw
            - snapshot.request_estimate_tokens
        )

        accounting = self.service.estimate_request(
            messages=grown_messages,
            tools=self.tools,
            usage_snapshot=snapshot,
        )

        self.assertEqual(accounting.total_tokens, expected)
        self.assertTrue(accounting.used_usage_snapshot)

    def test_changed_tool_schema_invalidates_provider_snapshot(self):
        snapshot = self.service.observe_prompt_usage(
            messages=self.messages,
            tools=self.tools,
            prompt_tokens=123,
        )
        changed_tools = [
            *self.tools,
            {
                "type": "function",
                "function": {
                    "name": "fetch",
                    "parameters": {"type": "object"},
                },
            },
        ]
        expected = self.estimator.estimate_request(
            messages=self.messages,
            tools=changed_tools,
        )

        accounting = self.service.estimate_request(
            messages=self.messages,
            tools=changed_tools,
            usage_snapshot=snapshot,
        )

        self.assertEqual(accounting.total_tokens, expected)
        self.assertFalse(accounting.used_usage_snapshot)

    def test_low_confidence_snapshot_scales_request_shrink(self):
        estimator = HeuristicRequestTokenEstimator(
            protocol_overhead_tokens=0
        )
        service = TokenAccountingService(
            model="unknown/model",
            request_estimator=estimator,
        )
        long_messages = [
            {"role": "user", "content": "x" * 2_000},
        ]
        short_messages = [
            {"role": "user", "content": "short"},
        ]
        snapshot = service.observe_prompt_usage(
            messages=long_messages,
            tools=[],
            prompt_tokens=700,
        )
        expected = estimator.estimate_request(
            messages=short_messages,
            tools=[],
        )
        calibrated_expected = max(
            1,
            700 + expected - snapshot.request_estimate_tokens,
            math.ceil(
                700
                * expected
                / snapshot.request_estimate_tokens
            ),
        )

        accounting = service.estimate_request(
            messages=short_messages,
            tools=[],
            usage_snapshot=snapshot,
        )

        self.assertEqual(
            accounting.total_tokens,
            calibrated_expected,
        )
        self.assertTrue(accounting.used_usage_snapshot)

    def test_fixed_and_compactable_diagnostics_partition_total(self):
        accounting = self.service.estimate_request(
            messages=self.messages,
            tools=self.tools,
        )

        self.assertGreater(accounting.fixed_tokens, 0)
        self.assertGreater(accounting.compactable_tokens, 0)
        self.assertEqual(
            accounting.fixed_tokens + accounting.compactable_tokens,
            accounting.total_tokens,
        )


if __name__ == "__main__":
    unittest.main()
