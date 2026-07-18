"""Context-relative input and absolute output policy for one tool result."""

from typing import Literal

from .models import ResultBudgetDecision, ResultHandling


def estimate_untrusted_result_tokens(
    text: str,
    *,
    utf8_size_bytes: int | None = None,
) -> int:
    """Conservatively estimate tokens for arbitrary untrusted tool output."""
    chars_estimate = len(text)
    byte_count = (
        len(text.encode("utf-8"))
        if utf8_size_bytes is None
        else max(0, utf8_size_bytes)
    )
    utf8_bytes_estimate = (byte_count + 1) // 2
    return max(1, chars_estimate, utf8_bytes_estimate)


class ResultContextBudgetPolicy:
    """Choose a safe visible representation without performing I/O."""

    def __init__(
        self,
        *,
        context_window_tokens: int,
        reserved_output_tokens: int | None,
        max_output_tokens: int,
        context_safety_ratio: float,
        context_compaction_target_ratio: float,
        inline_result_max_input_ratio: float,
        single_pass_summary_max_input_ratio: float,
        result_summary_target_tokens: int,
        result_compaction_max_output_tokens: int,
        max_in_memory_content_bytes: int,
    ):
        self.context_window_tokens = context_window_tokens
        self.reserved_output_tokens = reserved_output_tokens
        self.max_output_tokens = max_output_tokens
        self.context_safety_ratio = context_safety_ratio
        self.context_compaction_target_ratio = context_compaction_target_ratio
        self.inline_result_max_input_ratio = inline_result_max_input_ratio
        self.single_pass_summary_max_input_ratio = (
            single_pass_summary_max_input_ratio
        )
        self.result_summary_target_tokens = result_summary_target_tokens
        self.result_compaction_max_output_tokens = (
            result_compaction_max_output_tokens
        )
        self.max_in_memory_content_bytes = max_in_memory_content_bytes

        effective_reserved = max(
            reserved_output_tokens or 0,
            max_output_tokens,
        )
        self.usable_input_tokens = max(
            1,
            context_window_tokens - effective_reserved,
        )
        self.trigger_tokens = max(
            1,
            int(self.usable_input_tokens * context_safety_ratio),
        )
        self.target_tokens = max(
            1,
            int(
                self.usable_input_tokens
                * context_compaction_target_ratio
            ),
        )
        self.inline_limit_tokens = max(
            1,
            int(
                self.usable_input_tokens
                * inline_result_max_input_ratio
            ),
        )
        self.single_pass_limit_tokens = max(
            1,
            int(
                self.usable_input_tokens
                * single_pass_summary_max_input_ratio
            ),
        )
        self.compactor_output_tokens = min(
            result_compaction_max_output_tokens,
            max_output_tokens,
        )
        self.summary_target_tokens = min(
            result_summary_target_tokens,
            self.compactor_output_tokens,
        )

    def decide(
        self,
        *,
        handling: ResultHandling,
        current_context_tokens: int,
        result_tokens: int,
        result_size_bytes: int,
        summary_request_overhead_tokens: int,
        enable_result_compaction: bool,
        tool_message_overhead_tokens: int = 64,
    ) -> ResultBudgetDecision:
        current_context_tokens = max(0, current_context_tokens)
        result_tokens = max(0, result_tokens)
        result_size_bytes = max(0, result_size_bytes)
        summary_request_overhead_tokens = max(
            0,
            summary_request_overhead_tokens,
        )
        candidate_context_tokens = (
            current_context_tokens
            + result_tokens
            + max(0, tool_message_overhead_tokens)
        )
        available_before_trigger = max(
            0,
            self.trigger_tokens - current_context_tokens,
        )
        memory_safe = (
            result_size_bytes <= self.max_in_memory_content_bytes
        )
        inline_safe = (
            result_tokens <= self.inline_limit_tokens
            and candidate_context_tokens < self.trigger_tokens
            and memory_safe
        )
        summary_safe = (
            memory_safe
            and (
                result_tokens + summary_request_overhead_tokens
                <= self.single_pass_limit_tokens
            )
            and enable_result_compaction
        )

        representation: Literal[
            "inline",
            "summarize",
            "store_only",
            "oversized",
        ]
        reason: str
        runtime_override = False

        if handling == ResultHandling.STORE_ONLY:
            representation = "store_only"
            reason = "store_only_requested"
        elif handling == ResultHandling.COMPACT:
            if not enable_result_compaction:
                representation = "store_only"
                reason = "compaction_disabled"
                runtime_override = True
            elif summary_safe:
                representation = "summarize"
                reason = "compact_requested"
            else:
                representation = "oversized"
                reason = self._unsafe_summary_reason(
                    memory_safe=memory_safe,
                    result_tokens=result_tokens,
                    summary_request_overhead_tokens=(
                        summary_request_overhead_tokens
                    ),
                )
                runtime_override = True
        elif inline_safe:
            representation = "inline"
            reason = "safe_inline"
        elif not enable_result_compaction:
            representation = "store_only"
            reason = "compaction_disabled_unsafe_inline"
            runtime_override = handling == ResultHandling.PREFER_INLINE
        elif summary_safe:
            representation = "summarize"
            reason = "inline_budget_exceeded"
            runtime_override = handling == ResultHandling.PREFER_INLINE
        else:
            representation = "oversized"
            reason = self._unsafe_summary_reason(
                memory_safe=memory_safe,
                result_tokens=result_tokens,
                summary_request_overhead_tokens=(
                    summary_request_overhead_tokens
                ),
            )
            runtime_override = handling == ResultHandling.PREFER_INLINE

        return ResultBudgetDecision(
            representation=representation,
            reason=reason,
            runtime_override=runtime_override,
            current_context_tokens=current_context_tokens,
            result_tokens=result_tokens,
            candidate_context_tokens=candidate_context_tokens,
            usable_input_tokens=self.usable_input_tokens,
            trigger_tokens=self.trigger_tokens,
            available_before_trigger=available_before_trigger,
            inline_limit_tokens=self.inline_limit_tokens,
            single_pass_limit_tokens=self.single_pass_limit_tokens,
            summary_target_tokens=self.summary_target_tokens,
            compactor_output_tokens=self.compactor_output_tokens,
        )

    def _unsafe_summary_reason(
        self,
        *,
        memory_safe: bool,
        result_tokens: int,
        summary_request_overhead_tokens: int,
    ) -> str:
        if not memory_safe:
            return "technical_memory_limit_exceeded"
        if (
            result_tokens + summary_request_overhead_tokens
            > self.single_pass_limit_tokens
        ):
            return "single_pass_summary_limit_exceeded"
        return "unsafe_inline"
