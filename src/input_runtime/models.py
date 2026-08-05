"""Domain models and opaque identifiers for the input runtime."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_PATTERNS = {
    "admission": re.compile(r"^adm_[0-9a-f]{32}$"),
    "inbox": re.compile(r"^inbx_[0-9a-f]{32}$"),
    "control": re.compile(r"^ctl_[0-9a-f]{32}$"),
    "context": re.compile(r"^ctxrev_[0-9a-f]{32}$"),
    "emission": re.compile(r"^emit_[0-9a-f]{32}$"),
    "finalization": re.compile(r"^fin_[0-9a-f]{32}$"),
}


def _new(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def new_input_admission_id() -> str: return _new("adm")
def new_cycle_inbox_item_id() -> str: return _new("inbx")
def new_session_control_id() -> str: return _new("ctl")
def new_context_revision_id() -> str: return _new("ctxrev")
def new_agent_emission_id() -> str: return _new("emit")
def new_finalization_id() -> str: return _new("fin")

def _matches(name: str, value: str) -> bool:
    return isinstance(value, str) and bool(_PATTERNS[name].fullmatch(value))

def is_input_admission_id(value: str) -> bool: return _matches("admission", value)
def is_cycle_inbox_item_id(value: str) -> bool: return _matches("inbox", value)
def is_session_control_id(value: str) -> bool: return _matches("control", value)
def is_context_revision_id(value: str) -> bool: return _matches("context", value)
def is_agent_emission_id(value: str) -> bool: return _matches("emission", value)
def is_finalization_id(value: str) -> bool: return _matches("finalization", value)


def _required(value: str, name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must not be empty")
    return normalized


def _optional(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip() or None


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _dedupe(values: list[str], name: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _required(value, name)
        if normalized not in seen:
            result.append(normalized)
            seen.add(normalized)
    return result


class _InputRuntimeModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CycleRuntimeStatus(str, Enum):
    IDLE="idle"; RUNNING="running"; WAITING_USER="waiting_user"; PAUSE_REQUESTED="pause_requested"; PAUSED_BY_USER="paused_by_user"; INTERRUPTED="interrupted"; FINALIZING="finalizing"; DONE="done"; ERROR="error"; CANCELLED="cancelled"
class InputAdmissionKind(str, Enum):
    START_CYCLE="start_cycle"; CONTINUE_RUNNING="continue_running"; RESUME_WAITING="resume_waiting"; QUEUE_PAUSED="queue_paused"; RESUME_INTERRUPTED="resume_interrupted"
class InputAdmissionState(str, Enum):
    ADMITTED="admitted"; APPLIED="applied"; CANCELLED="cancelled"; FAILED_TERMINAL="failed_terminal"
class InputAdmissionAction(str, Enum):
    START_CYCLE="start_cycle"; QUEUED_RUNNING="queued_running"; RESUME_WAITING="resume_waiting"; QUEUED_PAUSED="queued_paused"; RESUME_INTERRUPTED="resume_interrupted"; DUPLICATE="duplicate"; CAPACITY_BLOCKED="capacity_blocked"
class CycleInboxState(str, Enum):
    QUEUED="queued"; CLAIMED="claimed"; APPLYING="applying"; APPLIED="applied"; CANCELLED="cancelled"; FAILED_TERMINAL="failed_terminal"
class SessionControlKind(str, Enum):
    PAUSE="pause"; CONTINUE="continue"; RESET="reset"
class SessionControlState(str, Enum):
    QUEUED="queued"; ACKNOWLEDGED="acknowledged"; APPLIED="applied"; REJECTED="rejected"; CANCELLED="cancelled"
class ControlAction(str, Enum):
    QUEUED="queued"; ALREADY_PENDING="already_pending"; ALREADY_EFFECTIVE="already_effective"; APPLIED="applied"; REJECTED="rejected"; CANCELLED="cancelled"
class ContextRevisionReason(str, Enum):
    INITIAL_INPUT="initial_input"; INPUT_APPLIED="input_applied"; RESUMED="resumed"; RECOVERED="recovered"
class AgentEmissionKind(str, Enum):
    INTERMEDIATE="intermediate"; RUNTIME_NOTICE="runtime_notice"; QUESTION="question"
class AgentEmissionVisibility(str, Enum):
    USER="user"; DEBUG="debug"; INTERNAL="internal"
class AgentEmissionImportance(str, Enum):
    NORMAL="normal"; HIGH="high"
class AgentEmissionState(str, Enum):
    READY="ready"; DELIVERING="delivering"; DELIVERED="delivered"; FAILED="failed"; UNKNOWN="unknown"; CANCELLED="cancelled"
class FinalizationState(str, Enum):
    PREPARED="prepared"; ABORTED_NEW_INPUT="aborted_new_input"; ABORTED_CONTROL="aborted_control"; RESULT_PERSISTED="result_persisted"; OUTPUT_READY="output_ready"; TERMINAL_COMMITTED="terminal_committed"; FAILED_RECOVERABLE="failed_recoverable"; FAILED_TERMINAL="failed_terminal"
class CheckpointName(str, Enum):
    RESUME="resume"; BEFORE_LLM="before_llm"; AFTER_TOOL_BLOCK="after_tool_block"; BEFORE_WAITING="before_waiting"; BEFORE_FINAL_PROCESSING="before_final_processing"; BEFORE_TERMINAL_COMMIT="before_terminal_commit"; AFTER_INTERRUPTION="after_interruption"
class CheckpointDecision(str, Enum):
    CONTINUE="continue"; PAUSED="paused"; RESET="reset"; INPUT_APPLIED="input_applied"; NO_CHANGE="no_change"; INTERRUPTED="interrupted"


class SessionInputRuntimeState(_InputRuntimeModel):
    schema_version: Literal[1] = 1
    session_id: str
    generation: int = Field(ge=0)
    active_cycle_id: str | None = None
    cycle_status: CycleRuntimeStatus
    accepted_through_session_sequence: int = Field(ge=0)
    active_cycle_accepted_through_sequence: int = Field(ge=0)
    active_cycle_applied_through_sequence: int = Field(ge=0)
    pending_control_sequence: int = Field(ge=0)
    applied_control_sequence: int = Field(ge=0)
    active_context_revision_id: str | None = None
    finalization_id: str | None = None
    revision: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime

    @field_validator("session_id")
    @classmethod
    def validate_session_id(cls, value: str) -> str: return _required(value, "session_id")
    @field_validator("active_cycle_id")
    @classmethod
    def normalize_cycle_id(cls, value: str | None) -> str | None: return _optional(value)
    @field_validator("active_context_revision_id")
    @classmethod
    def validate_context_id(cls, value: str | None) -> str | None:
        value = _optional(value)
        if value is not None and not is_context_revision_id(value): raise ValueError("invalid active_context_revision_id")
        return value
    @field_validator("finalization_id")
    @classmethod
    def validate_finalization_id(cls, value: str | None) -> str | None:
        value = _optional(value)
        if value is not None and not is_finalization_id(value): raise ValueError("invalid finalization_id")
        return value
    @field_validator("created_at", "updated_at")
    @classmethod
    def validate_timestamp(cls, value: datetime) -> datetime: return _utc(value)
    @model_validator(mode="after")
    def validate_state(self):
        if self.active_cycle_applied_through_sequence > self.active_cycle_accepted_through_sequence: raise ValueError("applied input watermark exceeds accepted watermark")
        if self.applied_control_sequence > self.pending_control_sequence: raise ValueError("applied control watermark exceeds pending watermark")
        if self.updated_at < self.created_at: raise ValueError("updated_at must not precede created_at")
        if self.cycle_status is CycleRuntimeStatus.IDLE:
            if any((self.active_cycle_id, self.active_context_revision_id, self.finalization_id)) or self.active_cycle_accepted_through_sequence or self.active_cycle_applied_through_sequence: raise ValueError("idle state must not reference an active cycle")
        active = {CycleRuntimeStatus.RUNNING, CycleRuntimeStatus.WAITING_USER, CycleRuntimeStatus.PAUSE_REQUESTED, CycleRuntimeStatus.PAUSED_BY_USER, CycleRuntimeStatus.INTERRUPTED, CycleRuntimeStatus.FINALIZING}
        if self.cycle_status in active and not self.active_cycle_id: raise ValueError("active cycle status requires active_cycle_id")
        if self.cycle_status is CycleRuntimeStatus.FINALIZING and not self.finalization_id: raise ValueError("finalizing requires finalization_id")
        if self.cycle_status is CycleRuntimeStatus.DONE and self.active_cycle_applied_through_sequence != self.active_cycle_accepted_through_sequence: raise ValueError("done requires all accepted input to be applied")
        return self


class InputAdmissionRecord(_InputRuntimeModel):
    schema_version: Literal[1] = 1
    admission_id: str
    session_id: str
    input_batch_id: str
    session_sequence: int = Field(ge=1)
    target_cycle_id: str
    cycle_sequence: int = Field(ge=0)
    admitted_generation: int = Field(ge=0)
    admission_kind: InputAdmissionKind
    state: InputAdmissionState
    idempotency_key: str
    admitted_at: datetime
    applied_at: datetime | None = None
    cancelled_at: datetime | None = None
    failure_code: str | None = None

    @field_validator("admission_id")
    @classmethod
    def validate_admission_id(cls, value: str) -> str:
        if not is_input_admission_id(value): raise ValueError("invalid admission_id")
        return value
    @field_validator("session_id", "input_batch_id", "target_cycle_id", "idempotency_key")
    @classmethod
    def validate_required(cls, value: str, info): return _required(value, info.field_name)
    @field_validator("failure_code")
    @classmethod
    def normalize_failure(cls, value: str | None) -> str | None: return _optional(value)
    @field_validator("admitted_at", "applied_at", "cancelled_at")
    @classmethod
    def validate_timestamp(cls, value: datetime | None): return None if value is None else _utc(value)
    @model_validator(mode="after")
    def validate_record(self):
        if (self.admission_kind is InputAdmissionKind.START_CYCLE) != (self.cycle_sequence == 0): raise ValueError("start_cycle requires sequence 0; additions require sequence >= 1")
        if self.state is InputAdmissionState.APPLIED and self.applied_at is None: raise ValueError("applied requires applied_at")
        if self.state is InputAdmissionState.CANCELLED and self.cancelled_at is None: raise ValueError("cancelled requires cancelled_at")
        if self.state is InputAdmissionState.FAILED_TERMINAL and not self.failure_code: raise ValueError("failed_terminal requires failure_code")
        if self.state is not InputAdmissionState.APPLIED and self.applied_at is not None: raise ValueError("applied_at contradicts state")
        if self.state is not InputAdmissionState.CANCELLED and self.cancelled_at is not None: raise ValueError("cancelled_at contradicts state")
        return self


class InputAdmissionOutcome(_InputRuntimeModel):
    admission_id: str | None = None
    input_batch_id: str
    session_id: str
    target_cycle_id: str | None = None
    cycle_sequence: int | None = None
    action: InputAdmissionAction
    should_start_runner: bool
    should_wake_runner: bool
    user_projection_key: str
    retryable: bool = False
    reason_code: str | None = None

    @field_validator("admission_id")
    @classmethod
    def validate_admission_id(cls, value: str | None) -> str | None:
        value = _optional(value)
        if value is not None and not is_input_admission_id(value): raise ValueError("invalid admission_id")
        return value
    @field_validator("input_batch_id", "session_id", "user_projection_key")
    @classmethod
    def validate_required(cls, value: str, info): return _required(value, info.field_name)
    @field_validator("target_cycle_id", "reason_code")
    @classmethod
    def normalize_optional(cls, value: str | None): return _optional(value)
    @model_validator(mode="after")
    def validate_outcome(self):
        if self.action is InputAdmissionAction.START_CYCLE and (not self.target_cycle_id or self.cycle_sequence != 0 or not self.should_start_runner): raise ValueError("invalid start_cycle outcome")
        queued = {InputAdmissionAction.QUEUED_RUNNING, InputAdmissionAction.RESUME_WAITING, InputAdmissionAction.QUEUED_PAUSED, InputAdmissionAction.RESUME_INTERRUPTED}
        if self.action in queued and (not self.admission_id or not self.target_cycle_id or self.cycle_sequence is None or self.cycle_sequence < 1): raise ValueError("queued outcome requires admission, target cycle and positive sequence")
        if self.action is InputAdmissionAction.CAPACITY_BLOCKED and (not self.retryable or not self.reason_code): raise ValueError("capacity_blocked must be retryable with reason_code")
        if self.action is InputAdmissionAction.QUEUED_PAUSED and (self.should_start_runner or self.should_wake_runner): raise ValueError("queued_paused must not start or wake runner")
        return self


class CycleInboxItem(_InputRuntimeModel):
    schema_version: Literal[1] = 1
    inbox_item_id: str
    admission_id: str
    session_id: str
    cycle_id: str
    input_batch_id: str
    cycle_sequence: int = Field(ge=1)
    generation: int = Field(ge=0)
    state: CycleInboxState
    claim_token: str | None = None
    claim_expires_at: datetime | None = None
    attempt_count: int = Field(default=0, ge=0)
    last_error_code: str | None = None
    enqueued_at: datetime
    claimed_at: datetime | None = None
    applied_at: datetime | None = None
    cancelled_at: datetime | None = None

    @field_validator("inbox_item_id")
    @classmethod
    def validate_item_id(cls, value: str) -> str:
        if not is_cycle_inbox_item_id(value): raise ValueError("invalid inbox_item_id")
        return value
    @field_validator("admission_id")
    @classmethod
    def validate_admission_id(cls, value: str) -> str:
        if not is_input_admission_id(value): raise ValueError("invalid admission_id")
        return value
    @field_validator("session_id", "cycle_id", "input_batch_id")
    @classmethod
    def validate_required(cls, value: str, info): return _required(value, info.field_name)
    @field_validator("claim_token", "last_error_code")
    @classmethod
    def normalize_optional(cls, value: str | None): return _optional(value)
    @field_validator("enqueued_at", "claimed_at", "claim_expires_at", "applied_at", "cancelled_at")
    @classmethod
    def validate_timestamp(cls, value: datetime | None): return None if value is None else _utc(value)
    @model_validator(mode="after")
    def validate_record(self):
        active = self.state in {CycleInboxState.CLAIMED, CycleInboxState.APPLYING}
        if active and not (self.claim_token and self.claim_expires_at and self.claimed_at): raise ValueError("active claim requires token, expiry and claimed_at")
        if self.state is CycleInboxState.QUEUED and (self.claim_token or self.claim_expires_at or self.claimed_at): raise ValueError("queued item must not contain active claim")
        if self.claimed_at and self.claim_expires_at and self.claim_expires_at <= self.claimed_at: raise ValueError("claim expiry must follow claim time")
        if self.state is CycleInboxState.APPLIED and self.applied_at is None: raise ValueError("applied requires applied_at")
        if self.state is CycleInboxState.CANCELLED and self.cancelled_at is None: raise ValueError("cancelled requires cancelled_at")
        if self.state is CycleInboxState.FAILED_TERMINAL and not self.last_error_code: raise ValueError("failed_terminal requires last_error_code")
        return self


class ClaimedInboxRange(_InputRuntimeModel):
    claim_token: str
    session_id: str
    cycle_id: str
    generation: int = Field(ge=0)
    items: list[CycleInboxItem] = Field(min_length=1)
    first_sequence: int = Field(ge=1)
    last_sequence: int = Field(ge=1)
    total_items: int = Field(ge=1)
    estimated_total_bytes: int = Field(ge=0)
    claimed_at: datetime
    claim_expires_at: datetime

    @field_validator("claim_token", "session_id", "cycle_id")
    @classmethod
    def validate_required(cls, value: str, info): return _required(value, info.field_name)
    @field_validator("claimed_at", "claim_expires_at")
    @classmethod
    def validate_timestamp(cls, value: datetime): return _utc(value)
    @model_validator(mode="after")
    def validate_range(self):
        if self.claim_expires_at <= self.claimed_at: raise ValueError("claim expiry must follow claim time")
        if self.total_items != len(self.items): raise ValueError("total_items must match items")
        sequences = [item.cycle_sequence for item in self.items]
        if sequences != list(range(sequences[0], sequences[0] + len(sequences))): raise ValueError("claimed sequence must be contiguous")
        if self.first_sequence != sequences[0] or self.last_sequence != sequences[-1]: raise ValueError("range bounds must match items")
        for item in self.items:
            if item.state is not CycleInboxState.CLAIMED or item.claim_token != self.claim_token or item.session_id != self.session_id or item.cycle_id != self.cycle_id or item.generation != self.generation: raise ValueError("claimed items must share claim identity")
        return self


class SessionControlCommand(_InputRuntimeModel):
    schema_version: Literal[1] = 1
    control_id: str
    session_id: str
    target_cycle_id: str | None = None
    generation: int = Field(ge=0)
    sequence_number: int = Field(ge=1)
    command: SessionControlKind
    state: SessionControlState
    idempotency_key: str
    source_client_type: str
    source_message_ref: dict[str, Any] | None = None
    reason: str | None = None
    created_at: datetime
    acknowledged_at: datetime | None = None
    applied_at: datetime | None = None
    rejected_at: datetime | None = None
    rejection_code: str | None = None

    @field_validator("control_id")
    @classmethod
    def validate_control_id(cls, value: str) -> str:
        if not is_session_control_id(value): raise ValueError("invalid control_id")
        return value
    @field_validator("session_id", "idempotency_key", "source_client_type")
    @classmethod
    def validate_required(cls, value: str, info): return _required(value, info.field_name)
    @field_validator("target_cycle_id", "reason", "rejection_code")
    @classmethod
    def normalize_optional(cls, value: str | None): return _optional(value)
    @field_validator("created_at", "acknowledged_at", "applied_at", "rejected_at")
    @classmethod
    def validate_timestamp(cls, value: datetime | None): return None if value is None else _utc(value)
    @model_validator(mode="after")
    def validate_command(self):
        if self.command in {SessionControlKind.PAUSE, SessionControlKind.CONTINUE} and not self.target_cycle_id: raise ValueError("pause and continue require target_cycle_id")
        if self.state is SessionControlState.ACKNOWLEDGED and self.acknowledged_at is None: raise ValueError("acknowledged requires acknowledged_at")
        if self.state is SessionControlState.APPLIED and (self.acknowledged_at is None or self.applied_at is None): raise ValueError("applied requires acknowledged_at and applied_at")
        if self.state is SessionControlState.REJECTED and (not self.rejection_code or self.rejected_at is None): raise ValueError("rejected requires rejection_code and rejected_at")
        return self


class ControlOutcome(_InputRuntimeModel):
    control_id: str | None = None
    session_id: str
    target_cycle_id: str | None = None
    command: SessionControlKind
    action: ControlAction
    effective_cycle_status: CycleRuntimeStatus | None = None
    should_wake_runner: bool
    user_projection_key: str
    retryable: bool = False
    reason_code: str | None = None

    @field_validator("control_id")
    @classmethod
    def validate_control_id(cls, value: str | None) -> str | None:
        value = _optional(value)
        if value is not None and not is_session_control_id(value): raise ValueError("invalid control_id")
        return value
    @field_validator("session_id", "user_projection_key")
    @classmethod
    def validate_required(cls, value: str, info): return _required(value, info.field_name)
    @field_validator("target_cycle_id", "reason_code")
    @classmethod
    def normalize_optional(cls, value: str | None): return _optional(value)


class ActiveCycleSnapshot(_InputRuntimeModel):
    schema_version: Literal[1] = 1
    cycle_id: str
    session_id: str
    generation: int = Field(ge=0)
    status: CycleRuntimeStatus
    original_input_batch_id: str
    original_user_request: str
    messages_for_llm: list[dict[str, Any]] = Field(default_factory=list)
    cycle_trace: list[dict[str, Any]] = Field(default_factory=list)
    working_memory_ref: str | None = None
    applied_input_batch_ids: list[str] = Field(default_factory=list)
    applied_through_cycle_sequence: int = Field(ge=0)
    active_context_revision_id: str
    waiting_question: str | None = None
    pause_reason: str | None = None
    interruption_reason: str | None = None
    active_plan_id: str | None = None
    active_plan_revision: int | None = Field(default=None, ge=1)
    active_plan_node_id: str | None = None
    artifact_refs: list[str] = Field(default_factory=list)
    read_artifact_refs: list[str] = Field(default_factory=list)
    result_refs: list[str] = Field(default_factory=list)
    config_revision: str | None = None
    snapshot_revision: int = Field(ge=1)
    safe_checkpoint: CheckpointName
    created_at: datetime
    updated_at: datetime

    @field_validator("cycle_id", "session_id", "original_input_batch_id", "original_user_request")
    @classmethod
    def validate_required(cls, value: str, info): return _required(value, info.field_name)
    @field_validator("active_context_revision_id")
    @classmethod
    def validate_context_id(cls, value: str) -> str:
        if not is_context_revision_id(value): raise ValueError("invalid active_context_revision_id")
        return value
    @field_validator("working_memory_ref", "waiting_question", "pause_reason", "interruption_reason", "active_plan_id", "active_plan_node_id", "config_revision")
    @classmethod
    def normalize_optional(cls, value: str | None): return _optional(value)
    @field_validator("applied_input_batch_ids", "artifact_refs", "read_artifact_refs", "result_refs")
    @classmethod
    def dedupe_ids(cls, values: list[str], info): return _dedupe(values, info.field_name)
    @field_validator("created_at", "updated_at")
    @classmethod
    def validate_timestamp(cls, value: datetime): return _utc(value)
    @model_validator(mode="after")
    def validate_snapshot(self):
        if self.updated_at < self.created_at: raise ValueError("updated_at must not precede created_at")
        if self.status is CycleRuntimeStatus.WAITING_USER and not self.waiting_question: raise ValueError("waiting_user requires waiting_question")
        if self.status is CycleRuntimeStatus.PAUSED_BY_USER and not self.pause_reason: raise ValueError("paused_by_user requires pause_reason")
        if self.status is CycleRuntimeStatus.INTERRUPTED and not self.interruption_reason: raise ValueError("interrupted requires interruption_reason")
        return self


class CycleContextRevision(_InputRuntimeModel):
    schema_version: Literal[1] = 1
    context_revision_id: str
    cycle_id: str
    session_id: str
    revision_number: int = Field(ge=1)
    parent_revision_ids: list[str] = Field(default_factory=list)
    reason: ContextRevisionReason
    applied_input_batch_ids: list[str] = Field(default_factory=list)
    applied_through_cycle_sequence: int = Field(ge=0)
    added_artifact_refs: list[str] = Field(default_factory=list)
    constraint_summary: str | None = None
    created_at: datetime

    @field_validator("context_revision_id")
    @classmethod
    def validate_context_id(cls, value: str) -> str:
        if not is_context_revision_id(value): raise ValueError("invalid context_revision_id")
        return value
    @field_validator("cycle_id", "session_id")
    @classmethod
    def validate_required(cls, value: str, info): return _required(value, info.field_name)
    @field_validator("parent_revision_ids")
    @classmethod
    def validate_parents(cls, values: list[str]) -> list[str]:
        values = _dedupe(values, "parent_revision_ids")
        if any(not is_context_revision_id(value) for value in values): raise ValueError("invalid parent context revision ID")
        return values
    @field_validator("applied_input_batch_ids", "added_artifact_refs")
    @classmethod
    def dedupe_ids(cls, values: list[str], info): return _dedupe(values, info.field_name)
    @field_validator("constraint_summary")
    @classmethod
    def normalize_summary(cls, value: str | None) -> str | None:
        value = _optional(value)
        if value is not None and len(value) > 10_000: raise ValueError("constraint_summary is too long")
        return value
    @field_validator("created_at")
    @classmethod
    def validate_timestamp(cls, value: datetime): return _utc(value)
    @model_validator(mode="after")
    def validate_revision(self):
        # Schema is ready for multiple parents, but merge semantics remain forbidden until v0.6.
        if len(self.parent_revision_ids) > 1: raise ValueError("multiple parents are not supported before v0.6")
        if self.reason is ContextRevisionReason.INITIAL_INPUT:
            if self.revision_number != 1 or self.parent_revision_ids or self.applied_through_cycle_sequence != 0: raise ValueError("initial_input must be parentless revision 1 at sequence 0")
        elif self.revision_number < 2 or len(self.parent_revision_ids) != 1:
            raise ValueError("non-initial revisions require exactly one parent")
        if self.reason is ContextRevisionReason.INPUT_APPLIED and not self.applied_input_batch_ids: raise ValueError("input_applied requires applied batches")
        return self


class AgentEmission(_InputRuntimeModel):
    schema_version: Literal[1] = 1
    emission_id: str
    session_id: str
    cycle_id: str
    context_revision_id: str
    kind: AgentEmissionKind
    text: str
    visibility: AgentEmissionVisibility
    importance: AgentEmissionImportance
    response_route: dict[str, Any]
    state: AgentEmissionState
    idempotency_key: str
    created_at: datetime
    delivered_at: datetime | None = None
    error_code: str | None = None

    @field_validator("emission_id")
    @classmethod
    def validate_emission_id(cls, value: str) -> str:
        if not is_agent_emission_id(value): raise ValueError("invalid emission_id")
        return value
    @field_validator("context_revision_id")
    @classmethod
    def validate_context_id(cls, value: str) -> str:
        if not is_context_revision_id(value): raise ValueError("invalid context_revision_id")
        return value
    @field_validator("session_id", "cycle_id", "text", "idempotency_key")
    @classmethod
    def validate_required(cls, value: str, info): return _required(value, info.field_name)
    @field_validator("error_code")
    @classmethod
    def normalize_error(cls, value: str | None): return _optional(value)
    @field_validator("created_at", "delivered_at")
    @classmethod
    def validate_timestamp(cls, value: datetime | None): return None if value is None else _utc(value)
    @model_validator(mode="after")
    def validate_emission(self):
        if self.visibility is AgentEmissionVisibility.USER and not self.response_route: raise ValueError("user-visible emission requires response_route")
        if self.state is AgentEmissionState.DELIVERED and self.delivered_at is None: raise ValueError("delivered requires delivered_at")
        if self.state in {AgentEmissionState.FAILED, AgentEmissionState.UNKNOWN} and not self.error_code: raise ValueError("failed/unknown emission requires error_code")
        return self


class AgentEmissionClaim(_InputRuntimeModel):
    claim_token: str
    emissions: list[AgentEmission] = Field(min_length=1)
    claimed_at: datetime
    claim_expires_at: datetime

    @field_validator("claim_token")
    @classmethod
    def validate_token(cls, value: str): return _required(value, "claim_token")
    @field_validator("claimed_at", "claim_expires_at")
    @classmethod
    def validate_timestamp(cls, value: datetime): return _utc(value)
    @model_validator(mode="after")
    def validate_claim(self):
        if self.claim_expires_at <= self.claimed_at: raise ValueError("claim expiry must follow claim time")
        if any(emission.state is not AgentEmissionState.DELIVERING for emission in self.emissions): raise ValueError("claimed emissions must be delivering")
        return self


class CycleFinalizationRecord(_InputRuntimeModel):
    schema_version: Literal[1] = 1
    finalization_id: str
    session_id: str
    cycle_id: str
    generation: int = Field(ge=0)
    context_revision_id: str
    expected_accepted_sequence: int = Field(ge=0)
    expected_applied_sequence: int = Field(ge=0)
    expected_control_sequence: int = Field(ge=0)
    state: FinalizationState
    result_ref: str | None = None
    output_batch_id: str | None = None
    failure_code: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator("finalization_id")
    @classmethod
    def validate_finalization_id(cls, value: str) -> str:
        if not is_finalization_id(value): raise ValueError("invalid finalization_id")
        return value
    @field_validator("context_revision_id")
    @classmethod
    def validate_context_id(cls, value: str) -> str:
        if not is_context_revision_id(value): raise ValueError("invalid context_revision_id")
        return value
    @field_validator("session_id", "cycle_id")
    @classmethod
    def validate_required(cls, value: str, info): return _required(value, info.field_name)
    @field_validator("result_ref", "output_batch_id", "failure_code")
    @classmethod
    def normalize_optional(cls, value: str | None): return _optional(value)
    @field_validator("created_at", "updated_at")
    @classmethod
    def validate_timestamp(cls, value: datetime): return _utc(value)
    @model_validator(mode="after")
    def validate_finalization(self):
        if self.updated_at < self.created_at: raise ValueError("updated_at must not precede created_at")
        if self.state is FinalizationState.PREPARED and self.expected_accepted_sequence != self.expected_applied_sequence: raise ValueError("prepared requires equal input watermarks")
        if self.state is FinalizationState.RESULT_PERSISTED and not self.result_ref: raise ValueError("result_persisted requires result_ref")
        if self.state is FinalizationState.OUTPUT_READY and (not self.result_ref or not self.output_batch_id): raise ValueError("output_ready requires result_ref and output_batch_id")
        if self.state is FinalizationState.TERMINAL_COMMITTED and not self.result_ref: raise ValueError("terminal_committed requires result_ref")
        if self.state in {FinalizationState.FAILED_RECOVERABLE, FinalizationState.FAILED_TERMINAL} and not self.failure_code: raise ValueError("failure state requires failure_code")
        return self


class CheckpointOutcome(_InputRuntimeModel):
    checkpoint: CheckpointName
    decision: CheckpointDecision
    context_revision_id: str | None = None
    applied_input_batch_ids: list[str] = Field(default_factory=list)
    applied_through_sequence: int = Field(ge=0)
    applied_control_ids: list[str] = Field(default_factory=list)
    should_continue: bool

    @field_validator("context_revision_id")
    @classmethod
    def validate_context_id(cls, value: str | None) -> str | None:
        value = _optional(value)
        if value is not None and not is_context_revision_id(value): raise ValueError("invalid context_revision_id")
        return value
    @field_validator("applied_input_batch_ids", "applied_control_ids")
    @classmethod
    def dedupe_ids(cls, values: list[str], info): return _dedupe(values, info.field_name)
    @model_validator(mode="after")
    def validate_outcome(self):
        if self.decision is CheckpointDecision.INPUT_APPLIED and (not self.applied_input_batch_ids or not self.context_revision_id): raise ValueError("input_applied requires batches and context revision")
        if self.decision in {CheckpointDecision.PAUSED, CheckpointDecision.RESET} and self.should_continue: raise ValueError("paused/reset must not continue")
        return self
