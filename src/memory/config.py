"""Configuration for runtime tool-result compaction."""

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class MemoryConfigType(BaseModel):
    """Result-compaction settings expressed relative to the model context."""

    model_config = ConfigDict(extra="forbid")

    enable_result_compaction: bool = True
    inline_result_max_input_ratio: float = 0.10
    single_pass_summary_max_input_ratio: float = 0.60
    result_summary_target_ratio: float = 0.01
    result_preview_max_chars: int = 4000
    cycle_compaction_summary_target_ratio: float = 0.02
    cycle_compaction_keep_recent_blocks: int = 2
    cycle_compaction_max_passes: int = 3

    @field_validator(
        "inline_result_max_input_ratio",
        "single_pass_summary_max_input_ratio",
        "result_summary_target_ratio",
        "cycle_compaction_summary_target_ratio",
    )
    @classmethod
    def validate_ratio(cls, value: float) -> float:
        if not 0 < value <= 1:
            raise ValueError("memory ratios must be in (0, 1]")
        return value

    @field_validator("result_preview_max_chars")
    @classmethod
    def validate_preview_limit(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("result_preview_max_chars must be positive")
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
    def validate_ratio_order(self):
        if (
            self.inline_result_max_input_ratio
            >= self.single_pass_summary_max_input_ratio
        ):
            raise ValueError(
                "inline_result_max_input_ratio must be lower than "
                "single_pass_summary_max_input_ratio"
            )
        if (
            self.result_summary_target_ratio
            >= self.single_pass_summary_max_input_ratio
        ):
            raise ValueError(
                "result_summary_target_ratio must be lower than "
                "single_pass_summary_max_input_ratio"
            )
        return self
