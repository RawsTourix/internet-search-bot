"""Context-budget accounting for attributed artifact composite compaction."""

from __future__ import annotations

import json
from typing import Any

from .artifact_composite_compaction import (
    ArtifactCompositeCompactionMixin,
    build_artifact_composite_compaction_prompt,
)


class ArtifactCompositeBudgetMixin:
    """Estimate the real prompt overhead of the specialized compactor."""

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
        if effective_tool_name not in ArtifactCompositeCompactionMixin._ARTIFACT_COMPOSITE_TYPES:
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
        request_payload = {
            "type": "artifact_composite_compaction_request",
            "request": request.model_dump(mode="json"),
            "expected_items": [],
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
