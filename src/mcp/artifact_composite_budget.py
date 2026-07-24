"""Context-budget accounting for attributed artifact composite compaction."""

from __future__ import annotations

import json
import math
from typing import Any

from ..memory import (
    TokenEstimateConfidence,
    TokenEstimateSource,
)
from .artifact_composite_compaction import (
    ArtifactCompositeCompactionMixin,
    build_artifact_composite_compaction_prompt,
)


class _SafetyMarginedResultTokenEstimator:
    """Use the configured model tokenizer with a conservative admission margin."""

    source = TokenEstimateSource.MODEL_TOKENIZER
    confidence = TokenEstimateConfidence.HIGH

    def __init__(
        self,
        primary,
        *,
        multiplier: float = 1.20,
        fixed_overhead_tokens: int = 32,
    ) -> None:
        self.primary = primary
        self.multiplier = multiplier
        self.fixed_overhead_tokens = fixed_overhead_tokens

    def _margin(self, estimate: int) -> int:
        return max(
            1,
            int(math.ceil(max(1, estimate) * self.multiplier))
            + self.fixed_overhead_tokens,
        )

    def estimate_text(self, text: str) -> int:
        return self._margin(self.primary.estimate_text(text))

    def estimate_messages(self, messages: list[dict[str, Any]]) -> int:
        return self._margin(self.primary.estimate_messages(messages))

    def estimate_request(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> int:
        return self._margin(
            self.primary.estimate_request(messages=messages, tools=tools)
        )


class ArtifactCompositeBudgetMixin:
    """Estimate composite results and the real specialized prompt overhead.

    Raw-result fidelity checks keep the strict conservative estimator. Admission
    to the configured model's compactor uses the model tokenizer when it is
    available with high confidence, plus an explicit safety margin. Heuristic
    estimators continue to fall back to the existing conservative policy.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        estimator = getattr(self, "request_token_estimator", None)
        if (
            getattr(estimator, "source", None)
            == TokenEstimateSource.MODEL_TOKENIZER
            and getattr(estimator, "confidence", None)
            == TokenEstimateConfidence.HIGH
        ):
            self.token_estimator = _SafetyMarginedResultTokenEstimator(estimator)

    def _result_summary_request_overhead_tokens(
        self,
        *,
        original_user_request: str,
        current_goal: str,
        effective_tool_name: str,
        effective_arguments: dict[str, Any],
        size_bytes: int,
        size_chars: int,
        size_tokens_estimate: int,
    ) -> int:
        if (
            effective_tool_name
            not in ArtifactCompositeCompactionMixin._ARTIFACT_COMPOSITE_TYPES
        ):
            return super()._result_summary_request_overhead_tokens(
                original_user_request=original_user_request,
                current_goal=current_goal,
                effective_tool_name=effective_tool_name,
                effective_arguments=effective_arguments,
                size_bytes=size_bytes,
                size_chars=size_chars,
                size_tokens_estimate=size_tokens_estimate,
            )

        request = self._result_summary_request(
            result_id="res_" + "0" * 32,
            original_user_request=original_user_request,
            current_goal=current_goal,
            effective_tool_name=effective_tool_name,
            effective_arguments=effective_arguments,
            size_bytes=size_bytes,
            size_chars=size_chars,
            size_tokens_estimate=size_tokens_estimate,
            summary_target_tokens=(
                self.result_budget_policy.summary_target_tokens
            ),
        )
        artifact_ids = effective_arguments.get("artifact_ids") or []
        if not isinstance(artifact_ids, list):
            artifact_ids = []
        expected_items = [
            {
                "request_index": index,
                "requested_artifact_id": str(artifact_id),
                "artifact_id": str(artifact_id),
                "filename": "artifact",
            }
            for index, artifact_id in enumerate(artifact_ids)
        ]
        request_payload = {
            "type": "artifact_composite_compaction_request",
            "request": request.model_dump(mode="json"),
            "expected_items": expected_items,
        }
        messages_without_raw = [
            {
                "role": "system",
                "content": build_artifact_composite_compaction_prompt(),
            },
            {
                "role": "user",
                "content": json.dumps(
                    request_payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
            {
                "role": "user",
                "content": (
                    "BEGIN_UNTRUSTED_TOOL_RESULT\n"
                    "\nEND_UNTRUSTED_TOOL_RESULT"
                ),
            },
        ]
        return self._estimate_request_tokens(
            messages=messages_without_raw,
            tools=[],
        )
