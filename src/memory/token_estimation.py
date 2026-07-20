"""Token estimation and provider-usage accounting for LLM requests."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol


logger = logging.getLogger(__name__)


class TokenEstimateSource(str, Enum):
    """Origin of one request-token estimate."""

    MODEL_TOKENIZER = "model_tokenizer"
    HEURISTIC = "heuristic"


class TokenEstimateConfidence(str, Enum):
    """How closely an estimate is expected to match the provider prompt."""

    HIGH = "high"
    LOW = "low"


class TokenEstimator(Protocol):
    """Estimate input tokens without coupling callers to one tokenizer."""

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


def _compact_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
    )


class ConservativeTokenEstimator:
    """Strict upper-biased estimator for arbitrary untrusted content.

    This estimator intentionally remains conservative because it is used for
    raw tool-result admission and fidelity checks. It must not be used as the
    primary auto-compaction trigger for a normal structured LLM request.
    """

    source = TokenEstimateSource.HEURISTIC
    confidence = TokenEstimateConfidence.LOW

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
        return self.estimate_text(_compact_json(messages))

    def estimate_request(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> int:
        payload = _compact_json({
            "messages": messages,
            "tools": tools,
        })
        return self.estimate_text(payload) + self.protocol_overhead_tokens


class HeuristicRequestTokenEstimator:
    """Model-neutral fallback for already structured LLM requests.

    The fallback is deliberately less aggressive than the raw-result
    estimator. UTF-8 density keeps it safer for non-ASCII text than a plain
    ``chars / 2`` rule while avoiding the pathological one-character-per-token
    estimate that caused premature cycle compaction.
    """

    source = TokenEstimateSource.HEURISTIC
    confidence = TokenEstimateConfidence.LOW

    def __init__(self, *, protocol_overhead_tokens: int = 32):
        if protocol_overhead_tokens < 0:
            raise ValueError("protocol_overhead_tokens must be non-negative")
        self.protocol_overhead_tokens = protocol_overhead_tokens

    def estimate_text(self, text: str) -> int:
        char_estimate = (len(text) + 1) // 2
        utf8_estimate = (len(text.encode("utf-8")) + 3) // 4
        return max(1, char_estimate, utf8_estimate)

    def estimate_messages(
        self,
        messages: list[dict[str, Any]],
    ) -> int:
        return self.estimate_text(_compact_json(messages))

    def estimate_request(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> int:
        payload = _compact_json({
            "messages": messages,
            "tools": tools,
        })
        return self.estimate_text(payload) + self.protocol_overhead_tokens


class TiktokenRequestTokenEstimator:
    """Model-aware request estimator backed by an optional tiktoken encoding."""

    source = TokenEstimateSource.MODEL_TOKENIZER
    confidence = TokenEstimateConfidence.HIGH

    def __init__(
        self,
        encoding: Any,
        *,
        encoding_name: str,
        protocol_overhead_tokens: int = 32,
        selection_source: str = "injected",
        requested_encoding: str | None = None,
        model_mapping_candidate: str | None = None,
    ):
        if protocol_overhead_tokens < 0:
            raise ValueError("protocol_overhead_tokens must be non-negative")
        self.encoding = encoding
        self.encoding_name = encoding_name
        self.protocol_overhead_tokens = protocol_overhead_tokens
        self.selection_source = selection_source
        self.requested_encoding = requested_encoding
        self.model_mapping_candidate = model_mapping_candidate

    @staticmethod
    def _model_candidates(model: str) -> list[str]:
        candidates = [model]
        if "/" in model:
            suffix = model.rsplit("/", 1)[-1]
            if suffix not in candidates:
                candidates.append(suffix)
        return candidates

    @classmethod
    def _resolve_model_encoding(
        cls,
        tiktoken_module: Any,
        model: str,
    ) -> tuple[Any, str]:
        last_error: Exception | None = None
        for candidate in cls._model_candidates(model):
            try:
                return (
                    tiktoken_module.encoding_for_model(candidate),
                    candidate,
                )
            except Exception as error:
                last_error = error
        if last_error is not None:
            raise last_error
        raise KeyError(f"No tokenizer mapping for model {model!r}")

    @classmethod
    def for_model(
        cls,
        model: str,
        *,
        encoding_name: str | None = None,
        protocol_overhead_tokens: int = 32,
    ) -> "TiktokenRequestTokenEstimator":
        import tiktoken

        model_mapping_candidate: str | None = None
        if encoding_name:
            try:
                encoding = tiktoken.get_encoding(encoding_name)
            except Exception as error:
                logger.warning(
                    "Configured tokenizer encoding unavailable; trying model "
                    "mapping: model=%s requested_encoding=%s error_type=%s",
                    model,
                    encoding_name,
                    type(error).__name__,
                )
                (
                    encoding,
                    model_mapping_candidate,
                ) = cls._resolve_model_encoding(tiktoken, model)
                resolved_name = str(
                    getattr(encoding, "name", "unknown"),
                )
                selection_source = (
                    "model_mapping_after_explicit_failure"
                )
            else:
                resolved_name = str(
                    getattr(encoding, "name", encoding_name),
                )
                selection_source = "explicit"
                try:
                    (
                        model_encoding,
                        model_candidate,
                    ) = cls._resolve_model_encoding(tiktoken, model)
                except Exception:
                    pass
                else:
                    model_encoding_name = str(
                        getattr(model_encoding, "name", "unknown"),
                    )
                    if model_encoding_name != resolved_name:
                        logger.warning(
                            "Configured tokenizer encoding differs from model "
                            "mapping; keeping explicit override: model=%s "
                            "requested_encoding=%s resolved_encoding=%s "
                            "model_encoding=%s model_candidate=%s",
                            model,
                            encoding_name,
                            resolved_name,
                            model_encoding_name,
                            model_candidate,
                        )
        else:
            (
                encoding,
                model_mapping_candidate,
            ) = cls._resolve_model_encoding(tiktoken, model)
            resolved_name = str(getattr(encoding, "name", "unknown"))
            selection_source = "model_mapping"

        return cls(
            encoding,
            encoding_name=resolved_name,
            protocol_overhead_tokens=protocol_overhead_tokens,
            selection_source=selection_source,
            requested_encoding=encoding_name,
            model_mapping_candidate=model_mapping_candidate,
        )

    def estimate_text(self, text: str) -> int:
        if not text:
            return 1
        tokens = self.encoding.encode(
            text,
            disallowed_special=(),
        )
        return max(1, len(tokens))

    def estimate_messages(
        self,
        messages: list[dict[str, Any]],
    ) -> int:
        return self.estimate_text(_compact_json(messages))

    def estimate_request(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> int:
        payload = _compact_json({
            "messages": messages,
            "tools": tools,
        })
        return self.estimate_text(payload) + self.protocol_overhead_tokens


def create_request_token_estimator(
    *,
    model: str,
    encoding_name: str | None = None,
    protocol_overhead_tokens: int = 32,
) -> TokenEstimator:
    """Prefer a known local tokenizer and degrade safely to a heuristic."""
    try:
        estimator = TiktokenRequestTokenEstimator.for_model(
            model,
            encoding_name=encoding_name,
            protocol_overhead_tokens=protocol_overhead_tokens,
        )
    except Exception as error:
        log_method = logger.warning if encoding_name else logger.info
        log_method(
            "No local tokenizer could be resolved; using request heuristic: "
            "model=%s requested_encoding=%s selection_source=heuristic "
            "error_type=%s",
            model,
            encoding_name,
            type(error).__name__,
        )
        return HeuristicRequestTokenEstimator(
            protocol_overhead_tokens=protocol_overhead_tokens,
        )

    logger.info(
        "Model tokenizer enabled: model=%s requested_encoding=%s "
        "resolved_encoding=%s selection_source=%s "
        "model_mapping_candidate=%s",
        model,
        estimator.requested_encoding,
        estimator.encoding_name,
        estimator.selection_source,
        estimator.model_mapping_candidate,
    )
    return estimator


@dataclass(frozen=True, slots=True)
class TokenUsageSnapshot:
    """Actual provider prompt usage tied to the request that produced it."""

    model: str
    prompt_tokens: int
    request_estimate_tokens: int
    request_fingerprint: str
    tool_schema_fingerprint: str
    estimator_source: str
    observed_at: float


@dataclass(frozen=True, slots=True)
class RequestTokenEstimate:
    """One full-request estimate with diagnostics for compaction policy."""

    total_tokens: int
    raw_estimate_tokens: int
    fixed_tokens: int
    compactable_tokens: int
    source: str
    confidence: str
    used_usage_snapshot: bool


class TokenAccountingService:
    """Combine model-aware counting with actual provider prompt snapshots."""

    def __init__(
        self,
        *,
        model: str,
        request_estimator: TokenEstimator,
    ):
        self.model = model
        self.request_estimator = request_estimator

    @property
    def source(self) -> str:
        value = getattr(
            self.request_estimator,
            "source",
            TokenEstimateSource.HEURISTIC,
        )
        return str(value.value if isinstance(value, Enum) else value)

    @property
    def confidence(self) -> str:
        value = getattr(
            self.request_estimator,
            "confidence",
            TokenEstimateConfidence.LOW,
        )
        return str(value.value if isinstance(value, Enum) else value)

    @staticmethod
    def _fingerprint(value: Any) -> str:
        payload = _compact_json(value).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def request_fingerprint(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> str:
        return self._fingerprint({
            "messages": messages,
            "tools": tools,
        })

    def tool_schema_fingerprint(
        self,
        tools: list[dict[str, Any]],
    ) -> str:
        return self._fingerprint(tools)

    def estimate_request(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        usage_snapshot: TokenUsageSnapshot | None = None,
    ) -> RequestTokenEstimate:
        raw_estimate = self.request_estimator.estimate_request(
            messages=messages,
            tools=tools,
        )
        total_tokens = raw_estimate
        used_snapshot = False

        request_fingerprint = self.request_fingerprint(
            messages=messages,
            tools=tools,
        )
        tool_fingerprint = self.tool_schema_fingerprint(tools)
        snapshot_matches = (
            usage_snapshot is not None
            and usage_snapshot.model == self.model
            and usage_snapshot.tool_schema_fingerprint == tool_fingerprint
        )
        if snapshot_matches:
            if usage_snapshot.request_fingerprint == request_fingerprint:
                total_tokens = usage_snapshot.prompt_tokens
                used_snapshot = True
            else:
                delta = (
                    raw_estimate
                    - usage_snapshot.request_estimate_tokens
                )
                additive = max(
                    1,
                    usage_snapshot.prompt_tokens + delta,
                )
                if self.confidence == TokenEstimateConfidence.LOW.value:
                    ratio_scaled = math.ceil(
                        usage_snapshot.prompt_tokens
                        * raw_estimate
                        / max(
                            1,
                            usage_snapshot.request_estimate_tokens,
                        )
                    )
                    total_tokens = max(
                        1,
                        additive,
                        ratio_scaled,
                    )
                else:
                    total_tokens = additive
                used_snapshot = True

        fixed_messages = [
            message
            for message in messages
            if message.get("role") == "system"
        ]
        fixed_tokens = min(
            total_tokens,
            self.request_estimator.estimate_request(
                messages=fixed_messages,
                tools=tools,
            ),
        )
        return RequestTokenEstimate(
            total_tokens=max(1, total_tokens),
            raw_estimate_tokens=max(1, raw_estimate),
            fixed_tokens=max(0, fixed_tokens),
            compactable_tokens=max(0, total_tokens - fixed_tokens),
            source=self.source,
            confidence=self.confidence,
            used_usage_snapshot=used_snapshot,
        )

    def observe_prompt_usage(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        prompt_tokens: int,
    ) -> TokenUsageSnapshot | None:
        if prompt_tokens < 1:
            return None
        request_estimate = self.request_estimator.estimate_request(
            messages=messages,
            tools=tools,
        )
        return TokenUsageSnapshot(
            model=self.model,
            prompt_tokens=prompt_tokens,
            request_estimate_tokens=request_estimate,
            request_fingerprint=self.request_fingerprint(
                messages=messages,
                tools=tools,
            ),
            tool_schema_fingerprint=self.tool_schema_fingerprint(tools),
            estimator_source=self.source,
            observed_at=time.time(),
        )
