"""Generic fidelity policy for compacted tool results."""

from __future__ import annotations

import json
from typing import Any, Protocol

from .models import ResultCompactionSummary
from .token_estimation import ConservativeTokenEstimator, TokenEstimator


class ResultFidelityPolicy(Protocol):
    """Apply runtime-owned, domain-neutral summary safeguards."""

    def apply(
        self,
        *,
        raw_result: str,
        summary: ResultCompactionSummary,
    ) -> ResultCompactionSummary:
        """Return a summary with conservative retrieval metadata."""


class ConservativeResultFidelityPolicy:
    """Require retrieval for substantially reduced record collections."""

    DEFAULT_LIMITATION = (
        "Краткое описание существенно сокращает структурированную "
        "коллекцию из нескольких записей; детали отдельных записей "
        "нужно сверять с сохранённым оригиналом."
    )

    def __init__(
        self,
        *,
        token_estimator: TokenEstimator | None = None,
        substantial_reduction_ratio: float = 0.5,
    ):
        if not 0 < substantial_reduction_ratio < 1:
            raise ValueError(
                "substantial_reduction_ratio must be in (0, 1)"
            )
        self.token_estimator = (
            token_estimator or ConservativeTokenEstimator()
        )
        self.substantial_reduction_ratio = substantial_reduction_ratio

    @staticmethod
    def _contains_multiple_records(value: Any) -> bool:
        pending = [value]
        while pending:
            current = pending.pop()
            if isinstance(current, list):
                if (
                    len(current) > 1
                    and all(isinstance(item, dict) for item in current)
                ):
                    return True
                pending.extend(current)
            elif isinstance(current, dict):
                pending.extend(current.values())
        return False

    def apply(
        self,
        *,
        raw_result: str,
        summary: ResultCompactionSummary,
    ) -> ResultCompactionSummary:
        try:
            parsed = json.loads(raw_result)
        except Exception:
            return summary

        if not self._contains_multiple_records(parsed):
            return summary

        raw_tokens = self.token_estimator.estimate_text(raw_result)
        compacted_tokens = self.token_estimator.estimate_text(
            summary.model_dump_json()
        )
        if (
            compacted_tokens
            > raw_tokens * self.substantial_reduction_ratio
        ):
            return summary

        limitations = list(summary.limitations)
        if self.DEFAULT_LIMITATION not in limitations:
            if len(limitations) >= 50:
                limitations[-1] = self.DEFAULT_LIMITATION
            else:
                limitations.append(self.DEFAULT_LIMITATION)
        return summary.model_copy(update={
            "limitations": limitations,
            "needs_original_content": True,
        })
