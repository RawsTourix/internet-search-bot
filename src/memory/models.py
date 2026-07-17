"""Models shared by result-budget policy and runtime orchestration."""

from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..storage.models import ContentRef, StoredResultRef


class ResultHandling(str, Enum):
    AUTO = "auto"
    PREFER_INLINE = "prefer_inline"
    COMPACT = "compact"
    STORE_ONLY = "store_only"


class ResultCompactionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["result_compaction"] = "result_compaction"
    summary: str
    key_facts: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    suggested_follow_up: list[str] = Field(default_factory=list)
    needs_original_content: bool = False

    @field_validator("summary")
    @classmethod
    def normalize_summary(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("summary must not be empty")
        return value

    @field_validator(
        "key_facts",
        "limitations",
        "suggested_follow_up",
        mode="before",
    )
    @classmethod
    def normalize_string_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("summary collections must be lists")

        result: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                raise ValueError("summary collection items must be strings")
            item = item.strip()
            if item and item not in seen:
                result.append(item)
                seen.add(item)
            if len(result) >= 50:
                break
        return result


class ResultCompactionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["result_compaction_request"] = "result_compaction_request"
    original_user_request: str
    current_goal: str | None = None
    agent_activity: str | None = None
    active_plan_node: dict[str, Any] | None = None
    result_id: str
    tool_name: str
    tool_arguments: dict[str, Any]
    size_bytes: int = Field(ge=0)
    size_chars: int = Field(ge=0)
    size_tokens_estimate: int = Field(ge=0)
    summary_target_tokens: int = Field(gt=0)


@dataclass(frozen=True, slots=True)
class ResultBudgetDecision:
    representation: Literal[
        "inline",
        "summarize",
        "store_only",
        "oversized",
    ]
    reason: str
    runtime_override: bool
    current_context_tokens: int
    result_tokens: int
    candidate_context_tokens: int
    usable_input_tokens: int
    trigger_tokens: int
    available_before_trigger: int
    inline_limit_tokens: int
    single_pass_limit_tokens: int
    summary_target_tokens: int


@dataclass(slots=True)
class ResultProcessingOutcome:
    decision: ResultBudgetDecision
    visible_payload: dict[str, Any]
    content_ref: ContentRef | None = None
    stored_result_ref: StoredResultRef | None = None
    persistence_failed: bool = False
    summary_failed: bool = False
