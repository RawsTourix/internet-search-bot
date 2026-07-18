"""Configuration for runtime tool-result compaction."""

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class MemoryConfigType(BaseModel):
    """Memory settings with relative input and absolute output budgets."""

    model_config = ConfigDict(extra="forbid")

    enable_result_compaction: bool = True
    inline_result_max_input_ratio: float = 0.10
    single_pass_summary_max_input_ratio: float = 0.60
    result_summary_target_tokens: int = 256
    result_compaction_max_output_tokens: int = 2048
    result_preview_max_chars: int = 4000
    cycle_compaction_summary_target_tokens: int = 512
    cycle_compaction_max_output_tokens: int = 2048
    cycle_compaction_keep_recent_blocks: int = 2
    cycle_compaction_max_passes: int = 3

    @field_validator(
        "inline_result_max_input_ratio",
        "single_pass_summary_max_input_ratio",
    )
    @classmethod
    def validate_ratio(cls, value: float) -> float:
        if not 0 < value <= 1:
            raise ValueError("memory ratios must be in (0, 1]")
        return value

    @field_validator(
        "result_summary_target_tokens",
        "result_compaction_max_output_tokens",
        "result_preview_max_chars",
        "cycle_compaction_summary_target_tokens",
        "cycle_compaction_max_output_tokens",
    )
    @classmethod
    def validate_positive_limit(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("memory limits must be positive")
        return value

    @field_validator("cycle_compaction_keep_recent_blocks")
    @classmethod
    def validate_keep_recent_blocks(cls, value: int) -> int:
        if value < 1:
            raise ValueError(
                "cycle_compaction_keep_recent_blocks must be at least 1"
            )
        return value

    @field_validator("cycle_compaction_max_passes")
    @classmethod
    def validate_cycle_compaction_max_passes(cls, value: int) -> int:
        if not 1 <= value <= 10:
            raise ValueError(
                "cycle_compaction_max_passes must be between 1 and 10"
            )
        return value

    @model_validator(mode="after")
    def validate_budget_order(self):
        if (
            self.inline_result_max_input_ratio
            >= self.single_pass_summary_max_input_ratio
        ):
            raise ValueError(
                "inline_result_max_input_ratio must be lower than "
                "single_pass_summary_max_input_ratio"
            )
        if (
            self.result_summary_target_tokens
            >= self.result_compaction_max_output_tokens
        ):
            raise ValueError(
                "result_summary_target_tokens must be lower than "
                "result_compaction_max_output_tokens"
            )
        if (
            self.cycle_compaction_summary_target_tokens
            >= self.cycle_compaction_max_output_tokens
        ):
            raise ValueError(
                "cycle_compaction_summary_target_tokens must be lower than "
                "cycle_compaction_max_output_tokens"
            )
        return self
