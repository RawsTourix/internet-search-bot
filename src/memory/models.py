"""Serializable models shared by memory services and runtime orchestration."""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from ..storage.models import (
    ContentRef,
    StoredResultRef,
    is_artifact_id,
    is_content_id,
    is_result_id,
)


_MAX_WORKING_STATE_ITEMS = 100


def _normalize_string_collection(
    value: Any,
    *,
    field_name: str,
    max_items: int = _MAX_WORKING_STATE_ITEMS,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list")

    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{field_name} items must be strings")
        normalized = item.strip()
        if normalized and normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
        if len(result) >= max_items:
            break
    return result


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


class _CycleModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CycleMessageRange(_CycleModel):
    start: int = Field(ge=0)
    end_exclusive: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_range(self):
        if self.end_exclusive <= self.start:
            raise ValueError("end_exclusive must be greater than start")
        return self


class CycleWorkingState(_CycleModel):
    current_goal: str

    completed_actions: list[str] = Field(default_factory=list)
    confirmed_actions: list[str] = Field(default_factory=list)
    rejected_actions: list[str] = Field(default_factory=list)

    important_results: list[str] = Field(default_factory=list)
    important_decisions: list[str] = Field(default_factory=list)
    modified_files: list[str] = Field(default_factory=list)

    pending_confirmation: str | None = None
    errors_affecting_continuation: list[str] = Field(default_factory=list)

    active_plan_id: str | None = None
    active_plan_node_id: str | None = None

    result_refs: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)

    @field_validator("current_goal")
    @classmethod
    def normalize_current_goal(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("current_goal must not be empty")
        return value

    @field_validator(
        "pending_confirmation",
        "active_plan_id",
        "active_plan_node_id",
    )
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator(
        "completed_actions",
        "confirmed_actions",
        "rejected_actions",
        "important_results",
        "important_decisions",
        "modified_files",
        "errors_affecting_continuation",
        mode="before",
    )
    @classmethod
    def normalize_working_lists(cls, value: Any, info) -> list[str]:
        return _normalize_string_collection(
            value,
            field_name=info.field_name,
        )

    @field_validator("result_refs", mode="before")
    @classmethod
    def normalize_result_refs(cls, value: Any) -> list[str]:
        refs = _normalize_string_collection(
            value,
            field_name="result_refs",
        )
        if any(not is_result_id(ref) for ref in refs):
            raise ValueError("result_refs must contain opaque result IDs")
        return refs

    @field_validator("artifact_refs", mode="before")
    @classmethod
    def normalize_artifact_refs(cls, value: Any) -> list[str]:
        refs = _normalize_string_collection(
            value,
            field_name="artifact_refs",
        )
        if any(not is_artifact_id(ref) for ref in refs):
            raise ValueError("artifact_refs must contain opaque artifact IDs")
        return refs


class CycleWorkingMemory(_CycleModel):
    type: Literal["cycle_working_memory"] = "cycle_working_memory"

    generation: int = Field(ge=1)
    summary: str
    working_state: CycleWorkingState

    source_message_ranges: list[CycleMessageRange] = Field(
        default_factory=list
    )
    archived_segment_refs: list[str] = Field(default_factory=list)
    archived_segment_count: int = Field(ge=1)

    previous_generation: int | None = Field(default=None, ge=1)

    runtime_generated: Literal[True] = True
    derived_from_untrusted_data: Literal[True] = True

    @field_validator("summary")
    @classmethod
    def normalize_summary(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("summary must not be empty")
        return value

    @field_validator("archived_segment_refs", mode="before")
    @classmethod
    def normalize_archived_refs(cls, value: Any) -> list[str]:
        refs = _normalize_string_collection(
            value,
            field_name="archived_segment_refs",
        )
        if any(not is_content_id(ref) for ref in refs):
            raise ValueError(
                "archived_segment_refs must contain opaque content IDs"
            )
        return refs

    @model_validator(mode="after")
    def validate_generations_and_archive_count(self):
        if (
            self.previous_generation is not None
            and self.previous_generation >= self.generation
        ):
            raise ValueError(
                "previous_generation must be lower than generation"
            )
        if self.archived_segment_count < len(self.archived_segment_refs):
            raise ValueError(
                "archived_segment_count must cover archived_segment_refs"
            )
        return self


class CycleCompactionResult(_CycleModel):
    type: Literal["cycle_compaction_result"] = "cycle_compaction_result"
    summary: str
    working_state: CycleWorkingState

    @field_validator("summary")
    @classmethod
    def normalize_summary(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("summary must not be empty")
        return value


class CycleCompactionRequest(_CycleModel):
    type: Literal["cycle_compaction_request"] = "cycle_compaction_request"

    original_user_request: str
    previous_working_memory: CycleWorkingMemory | None = None

    active_plan_state: dict[str, Any] | None = None

    segment_content_id: str
    segment_message_count: int = Field(gt=0)
    segment_tokens_estimate: int = Field(gt=0)

    target_summary_tokens: int = Field(gt=0)
    preserve_rules: list[str] = Field(default_factory=list)

    @field_validator("original_user_request")
    @classmethod
    def normalize_original_request(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("original_user_request must not be empty")
        return value

    @field_validator("segment_content_id")
    @classmethod
    def validate_segment_content_id(cls, value: str) -> str:
        if not is_content_id(value):
            raise ValueError("invalid segment_content_id")
        return value

    @field_validator("preserve_rules", mode="before")
    @classmethod
    def normalize_preserve_rules(cls, value: Any) -> list[str]:
        return _normalize_string_collection(
            value,
            field_name="preserve_rules",
        )


class CycleSegmentArchive(_CycleModel):
    schema_version: Literal[1] = 1
    type: Literal["cycle_source_segment"] = "cycle_source_segment"

    cycle_id: str
    generation: int = Field(ge=1)

    source_message_range: CycleMessageRange
    messages: list[dict[str, Any]]

    created_at: datetime

    @field_validator("cycle_id")
    @classmethod
    def normalize_cycle_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("cycle_id must not be empty")
        return value

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return value.astimezone(timezone.utc)
