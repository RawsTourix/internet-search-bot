"""Pydantic domain models for optional DAG planning."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from ..storage.models import is_artifact_id, is_result_id


_PLAN_ID_RE = re.compile(r"^plan_[0-9a-f]{32}$")
_PLAN_NODE_ID_RE = re.compile(r"^pnode_[0-9a-f]{32}$")
_CLIENT_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def new_plan_id() -> str:
    return f"plan_{uuid4().hex}"


def new_plan_node_id() -> str:
    return f"pnode_{uuid4().hex}"


def is_plan_id(value: str) -> bool:
    return bool(_PLAN_ID_RE.fullmatch(value))


def is_plan_node_id(value: str) -> bool:
    return bool(_PLAN_NODE_ID_RE.fullmatch(value))


def is_plan_client_key(value: str) -> bool:
    return bool(_CLIENT_KEY_RE.fullmatch(value))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _dedupe_texts(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _validate_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


class PlanStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class PlanNodeStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


class PlanNodeKind(str, Enum):
    COLLECT = "collect"
    PROCESS = "process"
    EXECUTE = "execute"
    VALIDATE = "validate"
    COORDINATE = "coordinate"
    OTHER = "other"


class PlanNodeTransition(str, Enum):
    START = "start"
    COMPLETE = "complete"
    BLOCK = "block"
    FAIL = "fail"
    SKIP = "skip"
    RETRY = "retry"


class AgentActivity(str, Enum):
    PLANNING = "planning"
    COLLECTING = "collecting"
    PROCESSING = "processing"
    EXECUTING = "executing"
    VALIDATING = "validating"
    FINALIZING = "finalizing"


class _PlanningModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanNode(_PlanningModel):
    node_id: str
    key: str

    title: str
    objective: str
    kind: PlanNodeKind

    depends_on: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)

    status: PlanNodeStatus = PlanNodeStatus.PENDING
    attempt_count: int = Field(default=0, ge=0)

    outcome_summary: str | None = None
    status_reason: str | None = None

    result_refs: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)

    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @field_validator("node_id")
    @classmethod
    def validate_node_id(cls, value: str) -> str:
        if not is_plan_node_id(value):
            raise ValueError("invalid node_id")
        return value

    @field_validator("key")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if not is_plan_client_key(value):
            raise ValueError("invalid node key")
        return value

    @field_validator("title", "objective")
    @classmethod
    def validate_required_text(cls, value: str, info) -> str:
        return _normalize_text(value, info.field_name)

    @field_validator("outcome_summary", "status_reason", mode="before")
    @classmethod
    def normalize_optional_text(cls, value):
        return _normalize_optional_text(value)

    @field_validator("depends_on")
    @classmethod
    def validate_dependencies(cls, values: list[str], info) -> list[str]:
        result = _dedupe_texts(values)
        for value in result:
            if not is_plan_node_id(value):
                raise ValueError("depends_on contains invalid node_id")
        return result

    @field_validator("success_criteria")
    @classmethod
    def normalize_success_criteria(cls, values: list[str]) -> list[str]:
        return _dedupe_texts(values)

    @field_validator("result_refs")
    @classmethod
    def validate_result_refs(cls, values: list[str]) -> list[str]:
        result = _dedupe_texts(values)
        if any(not is_result_id(value) for value in result):
            raise ValueError("result_refs contains invalid result_id")
        return result

    @field_validator("artifact_refs")
    @classmethod
    def validate_artifact_refs(cls, values: list[str]) -> list[str]:
        result = _dedupe_texts(values)
        if any(not is_artifact_id(value) for value in result):
            raise ValueError("artifact_refs contains invalid artifact_id")
        return result

    @field_validator("created_at", "updated_at", "started_at", "finished_at")
    @classmethod
    def validate_timestamps(cls, value: datetime | None) -> datetime | None:
        return _validate_aware_utc(value) if value is not None else None

    @model_validator(mode="after")
    def validate_node_state(self) -> "PlanNode":
        if self.node_id in self.depends_on:
            raise ValueError("node cannot depend on itself")
        if self.status == PlanNodeStatus.PENDING:
            if self.finished_at is not None:
                raise ValueError("pending node cannot have finished_at")
        if self.status == PlanNodeStatus.IN_PROGRESS:
            if self.started_at is None:
                raise ValueError("in_progress node requires started_at")
            if self.finished_at is not None:
                raise ValueError("in_progress node cannot have finished_at")
        if self.status == PlanNodeStatus.DONE:
            if not self.outcome_summary:
                raise ValueError("done node requires outcome_summary")
            if self.finished_at is None:
                raise ValueError("done node requires finished_at")
        if self.status in {
            PlanNodeStatus.BLOCKED,
            PlanNodeStatus.FAILED,
            PlanNodeStatus.SKIPPED,
        } and not self.status_reason:
            raise ValueError(f"{self.status.value} node requires status_reason")
        if self.status in {
            PlanNodeStatus.DONE,
            PlanNodeStatus.SKIPPED,
        } and self.finished_at is None:
            raise ValueError(f"{self.status.value} node requires finished_at")
        return self


class AgentPlan(_PlanningModel):
    schema_version: Literal[1] = 1

    plan_id: str
    session_id: str
    cycle_id: str

    goal: str
    strategy: str | None = None

    status: PlanStatus = PlanStatus.ACTIVE
    revision: int = Field(ge=1)

    nodes: list[PlanNode]
    cancellation_reason: str | None = None

    created_at: datetime
    updated_at: datetime

    @field_validator("plan_id")
    @classmethod
    def validate_plan_id(cls, value: str) -> str:
        if not is_plan_id(value):
            raise ValueError("invalid plan_id")
        return value

    @field_validator("session_id", "cycle_id", "goal")
    @classmethod
    def validate_required_text(cls, value: str, info) -> str:
        return _normalize_text(value, info.field_name)

    @field_validator("strategy", "cancellation_reason", mode="before")
    @classmethod
    def normalize_optional_text(cls, value):
        return _normalize_optional_text(value)

    @field_validator("created_at", "updated_at")
    @classmethod
    def validate_timestamps(cls, value: datetime) -> datetime:
        return _validate_aware_utc(value)

    @model_validator(mode="after")
    def validate_plan_state(self) -> "AgentPlan":
        if not self.nodes:
            raise ValueError("plan must contain at least one node")
        node_ids = [node.node_id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("duplicate node_id")
        keys = [node.key for node in self.nodes]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate node key")
        if self.status == PlanStatus.CANCELLED and not self.cancellation_reason:
            raise ValueError("cancelled plan requires cancellation_reason")
        if self.status != PlanStatus.CANCELLED and self.cancellation_reason:
            raise ValueError("cancellation_reason is only valid for cancelled plan")
        return self


class PlanRef(_PlanningModel):
    plan_id: str
    cycle_id: str
    goal: str
    status: PlanStatus
    revision: int = Field(ge=1)
    node_count: int = Field(ge=0)
    updated_at: datetime

    @field_validator("plan_id")
    @classmethod
    def validate_plan_id(cls, value: str) -> str:
        if not is_plan_id(value):
            raise ValueError("invalid plan_id")
        return value

    @field_validator("cycle_id", "goal")
    @classmethod
    def validate_required_text(cls, value: str, info) -> str:
        return _normalize_text(value, info.field_name)

    @field_validator("updated_at")
    @classmethod
    def validate_updated_at(cls, value: datetime) -> datetime:
        return _validate_aware_utc(value)


class PlanStoreMetadata(_PlanningModel):
    schema_version: Literal[1] = 1
    plan_id: str
    session_id: str
    cycle_id: str
    current_revision: int = Field(ge=1)
    status: PlanStatus
    updated_at: datetime

    @field_validator("plan_id")
    @classmethod
    def validate_plan_id(cls, value: str) -> str:
        if not is_plan_id(value):
            raise ValueError("invalid plan_id")
        return value

    @field_validator("session_id", "cycle_id")
    @classmethod
    def validate_required_text(cls, value: str, info) -> str:
        return _normalize_text(value, info.field_name)

    @field_validator("updated_at")
    @classmethod
    def validate_updated_at(cls, value: datetime) -> datetime:
        return _validate_aware_utc(value)


class PlanNodeSummary(_PlanningModel):
    node_id: str
    key: str
    title: str
    kind: PlanNodeKind
    status: PlanNodeStatus


class PlanNodeCounts(_PlanningModel):
    total: int = Field(ge=0)
    pending: int = Field(ge=0)
    in_progress: int = Field(ge=0)
    blocked: int = Field(ge=0)
    done: int = Field(ge=0)
    failed: int = Field(ge=0)
    skipped: int = Field(ge=0)


class ActivePlanState(_PlanningModel):
    type: Literal["active_plan_state"] = "active_plan_state"

    plan_id: str
    revision: int = Field(ge=1)
    status: PlanStatus
    goal: str

    current_node: PlanNodeSummary | None = None
    ready_nodes: list[PlanNodeSummary] = Field(default_factory=list)
    counts: PlanNodeCounts

    stalled: bool = False
    blocked_node_ids: list[str] = Field(default_factory=list)
    failed_node_ids: list[str] = Field(default_factory=list)
    ready_nodes_truncated: bool = False


class CreatePlanNodeInput(_PlanningModel):
    client_key: str
    title: str
    objective: str
    kind: PlanNodeKind
    depends_on: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)

    @field_validator("client_key")
    @classmethod
    def validate_client_key(cls, value: str) -> str:
        if not is_plan_client_key(value):
            raise ValueError("invalid client_key")
        return value

    @field_validator("title", "objective")
    @classmethod
    def validate_required_text(cls, value: str, info) -> str:
        return _normalize_text(value, info.field_name)

    @field_validator("depends_on", "success_criteria")
    @classmethod
    def normalize_lists(cls, values: list[str]) -> list[str]:
        return _dedupe_texts(values)


class AddPlanNodeInput(_PlanningModel):
    client_key: str
    title: str
    objective: str
    kind: PlanNodeKind
    depends_on_node_ids: list[str] = Field(default_factory=list)
    depends_on_client_keys: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)

    @field_validator("client_key")
    @classmethod
    def validate_client_key(cls, value: str) -> str:
        if not is_plan_client_key(value):
            raise ValueError("invalid client_key")
        return value

    @field_validator("title", "objective")
    @classmethod
    def validate_required_text(cls, value: str, info) -> str:
        return _normalize_text(value, info.field_name)

    @field_validator("depends_on_node_ids")
    @classmethod
    def validate_dependency_ids(cls, values: list[str]) -> list[str]:
        result = _dedupe_texts(values)
        if any(not is_plan_node_id(value) for value in result):
            raise ValueError("depends_on_node_ids contains invalid node_id")
        return result

    @field_validator("depends_on_client_keys", "success_criteria")
    @classmethod
    def normalize_lists(cls, values: list[str]) -> list[str]:
        return _dedupe_texts(values)
