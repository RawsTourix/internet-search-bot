"""Conservative token estimates for LLM text and request payloads."""

from __future__ import annotations

import json
from typing import Any, Protocol


class TokenEstimator(Protocol):
    """Estimate input tokens without depending on one model tokenizer."""

    def estimate_text(self, text: str) -> int:
        """Estimate tokens for one text value."""

    def estimate_messages(
        self,
        messages: list[dict[str, Any]],
    ) -> int:
        """Estimate tokens for a serialized messages payload."""

    def estimate_request(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> int:
        """Estimate the complete input request, including tool schemas."""


class ConservativeTokenEstimator:
    """Model-neutral estimator biased toward avoiding context overflow."""

    def __init__(self, *, protocol_overhead_tokens: int = 32):
        if protocol_overhead_tokens < 0:
            raise ValueError("protocol_overhead_tokens must be non-negative")
        self.protocol_overhead_tokens = protocol_overhead_tokens

    def estimate_text(self, text: str) -> int:
        utf8_size = len(text.encode("utf-8"))
        return max(1, len(text), (utf8_size + 1) // 2)

    def estimate_messages(
        self,
        messages: list[dict[str, Any]],
    ) -> int:
        payload = json.dumps(
            messages,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return self.estimate_text(payload)

    def estimate_request(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> int:
        payload = json.dumps(
            {
                "messages": messages,
                "tools": tools,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return (
            self.estimate_text(payload)
            + self.protocol_overhead_tokens
        )
