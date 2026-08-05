"""Validated domain records for the durable input runtime."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class CycleStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED_BY_USER = "paused_by_user"
    INTERRUPTED = "interrupted"
    FINALIZING = "finalizing"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


class CheckpointName(StrEnum):
    RESUME = "CP-RESUME"
    BEFORE_LLM = "CP-BEFORE-LLM"
    AFTER_TOOL_BLOCK = "CP-AFTER-TOOL-BLOCK"
    BEFORE_WAITING = "CP-BEFORE-WAITING"
    BEFORE_FINAL_PROCESSING = "CP-BEFORE-FINAL-PROCESSING"
    BEFORE_TERMINAL_COMMIT = "CP-BEFORE-TERMINAL-COMMIT"
    AFTER_INTERRUPTION = "CP-AFTER-INTERRUPTION"


class AdmissionKind(StrEnum):
    START_CYCLE = "start_cycle"
    CONTINUE_RUNNING = "continue_running"
    RESUME_WAITING = "resume_waiting"
    QUEUE_PAUSED = "queue_paused"
    RESUME_INTERRUPTED = "resume_interrupted"


class AdmissionState(StrEnum):
    ADMITTED = "admitted"
    APPLIED = "applied"
    CANCELLED = "cancelled"
    FAILED_TERMINAL = "failed_terminal"


class InboxState(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    APPLYING = "applying"
    APPLIED = "applied"
    CANCELLED = "cancelled"
    FAILED_TERMINAL = "failed_terminal"


class ControlCommandType(StrEnum):
    PAUSE = "pause"
    CONTINUE = "continue"
    RESET = "reset"


class ControlState(StrEnum):
    QUEUED = "queued"
    ACKNOWLEDGED = "acknowledged"
    APPLIED = "applied"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class EmissionState(StrEnum):
    READY = "ready"
    DELIVERING = "delivering"
    DELIVERED = "delivered"
    FAILED = "failed"
    UNKNOWN = "unknown"
    CANCELLED = "cancelled"


class FinalizationState(StrEnum):
    PREPARED = "prepared"
    ABORTED_NEW_INPUT = "aborted_new_input"
    ABORTED_CONTROL = "aborted_control"
    RESULT_PERSISTED = "result_persisted"
    OUTPUT_READY = "output_ready"
    TERMINAL_COMMITTED = "terminal_committed"
    FAILED_RECOVERABLE = "failed_recoverable"
    FAILED_TERMINAL = "failed_terminal"


class CheckpointAction(StrEnum):
    CONTINUE = "continue"
    INPUT_APPLIED = "input_applied"
    PAUSE = "pause"
    WAIT = "wait"
    INTERRUPT = "interrupt"
    ABORT_FINALIZATION = "abort_finalization"


_ID_PATTERNS = {
    "admission_id": re.compile(r"^adm_[0-9a-f]{32}$"),
    "inbox_item_id": re.compile(r"^inbx_[0-9a-f]{32}$"),
    "control_id": re.compile(r"^ctl_[0-9a-f]{32}$"),
    "context_revision_id": re.compile(r"^ctxrev_[0-9a-f]{32}$"),
    "active_context_revision_id": re.compile(r"^ctxrev_[0-9a-f]{32}$"),
    "emission_id": re.compile(r"^emit_[0-9a-f]{32}$"),
    "finalization_id": re.compile(r"^fin_[0-9a-f]{32}$"),
}
TERMINAL_SESSION_STATUSES = frozenset({
    CycleStatus.DONE, CycleStatus.ERROR, CycleStatus.CANCELLED,
})


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def new_admission_id() -> str: return _new_id("adm")
def new_inbox_item_id() -> str: return _new_id("inbx")
def new_control_id() -> str: return _new_id("ctl")
def new_context_revision_id() -> str: return _new_id("ctxrev")
def new_emission_id() -> str: return _new_id("emit")
def new_finalization_id() -> str: return _new_id("fin")


class InputRuntimeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, use_enum_values=False)

    @field_validator(
        "created_at", "updated_at", "admitted_at", "applied_at",
        "cancelled_at", "enqueued_at", "claimed_at", "claim_expires_at",
        "acknowledged_at", "delivered_at", mode="before", check_fields=False,
    )
    @classmethod
    def validate_timestamp(cls, value: Any) -> Any:
        if value is None or not isinstance(value, datetime):
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("durable timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    @field_validator(*_ID_PATTERNS.keys(), check_fields=False)
    @classmethod
    def validate_stable_id(cls, value: str | None, info: Any) -> str | None:
        if value is None:
            return value
        if not _ID_PATTERNS[info.field_name].fullmatch(value):
            raise ValueError(f"invalid stable {info.field_name}")
        return value

    @field_validator(
        "session_id", "cycle_id", "target_cycle_id", "input_batch_id",
        "original_input_batch_id", "idempotency_key", "claim_token",
        "source_client_type", mode="before", check_fields=False,
    )
    @classmethod
    def normalize_required_string(cls, value: Any, info: Any) -> Any:
        if value is None and info.field_name == "target_cycle_id":
            return None
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{info.field_name} must be a non-empty string")
        return normalized

    @field_validator("parent_revision_ids", check_fields=False)
    @classmethod
    def validate_parent_revision_ids(cls, value: list[str]) -> list[str]:
        pattern = _ID_PATTERNS["context_revision_id"]
        if any(not pattern.fullmatch(item) for item in value):
            raise ValueError("parent_revision_ids must contain ctxrev IDs")
        if len(set(value)) != len(value):
            raise ValueError("parent_revision_ids must be unique")
        return value

    @classmethod
    def ensure_json_safe(cls, value: Any, field_name: str) -> Any:
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError(f"{field_name} must be JSON-safe") from error
        return value


class SessionInputRuntimeState(InputRuntimeModel):
    session_id: str
    generation: int = Field(ge=0)
    active_cycle_id: str | None = None
    cycle_status: CycleStatus = CycleStatus.IDLE
    accepted_through_session_sequence: int = Field(default=0, ge=0)
    active_cycle_accepted_through_sequence: int = Field(default=0, ge=0)
    active_cycle_applied_through_sequence: int = Field(default=0, ge=0)
    pending_control_sequence: int = Field(default=0, ge=0)
    applied_control_sequence: int = Field(default=0, ge=0)
    active_context_revision_id: str | None = None
    finalization_id: str | None = None
    revision: int = Field(default=1, ge=1)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_state(self) -> "SessionInputRuntimeState":
        if self.active_cycle_applied_through_sequence > self.active_cycle_accepted_through_sequence:
            raise ValueError("applied input watermark cannot exceed accepted watermark")
        if self.applied_control_sequence > self.pending_control_sequence:
            raise ValueError("applied control watermark cannot exceed pending watermark")
        if self.cycle_status == CycleStatus.IDLE and self.active_cycle_id is not None:
            raise ValueError("idle session cannot have an active cycle")
        if self.cycle_status not in {CycleStatus.IDLE, *TERMINAL_SESSION_STATUSES} and self.active_cycle_id is None:
            raise ValueError("active cycle status requires active_cycle_id")
        if self.cycle_status == CycleStatus.FINALIZING and self.finalization_id is None:
            raise ValueError("finalizing status requires finalization_id")
        if self.finalization_id is not None and self.cycle_status not in {CycleStatus.FINALIZING, *TERMINAL_SESSION_STATUSES}:
            raise ValueError("finalization_id is invalid for current status")
        if self.cycle_status in TERMINAL_SESSION_STATUSES:
            if self.active_cycle_applied_through_sequence != self.active_cycle_accepted_through_sequence:
                raise ValueError("terminal state requires equal input watermarks")
            if self.applied_control_sequence != self.pending_control_sequence:
                raise ValueError("terminal state requires equal control watermarks")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


class InputAdmissionRecord(InputRuntimeModel):
    admission_id: str = Field(default_factory=new_admission_id)
    session_id: str
    input_batch_id: str
    session_sequence: int = Field(ge=1)
    target_cycle_id: str
    cycle_sequence: int = Field(ge=0)
    admitted_generation: int = Field(ge=0)
    admission_kind: AdmissionKind
    state: AdmissionState = AdmissionState.ADMITTED
    idempotency_key: str
    admitted_at: datetime
    applied_at: datetime | None = None
    cancelled_at: datetime | None = None
    failure_code: str | None = None

    @model_validator(mode="after")
    def validate_state(self) -> "InputAdmissionRecord":
        if (self.admission_kind == AdmissionKind.START_CYCLE) != (self.cycle_sequence == 0):
            raise ValueError("only start_cycle admission may use cycle_sequence=0")
        if self.state == AdmissionState.APPLIED and self.applied_at is None:
            raise ValueError("applied admission requires applied_at")
        if self.state != AdmissionState.APPLIED and self.applied_at is not None:
            raise ValueError("applied_at is only valid for applied admission")
        if self.state == AdmissionState.CANCELLED and self.cancelled_at is None:
            raise ValueError("cancelled admission requires cancelled_at")
        if self.state != AdmissionState.CANCELLED and self.cancelled_at is not None:
            raise ValueError("cancelled_at is only valid for cancelled admission")
        if self.state == AdmissionState.FAILED_TERMINAL and not self.failure_code:
            raise ValueError("failed_terminal admission requires failure_code")
        if self.state != AdmissionState.FAILED_TERMINAL and self.failure_code is not None:
            raise ValueError("failure_code is only valid for failed_terminal admission")
        return self


class InputAdmissionOutcome(InputRuntimeModel):
    outcome: str
    admission: InputAdmissionRecord | None = None
    retryable: bool = False
    reason_code: str | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> "InputAdmissionOutcome":
        allowed = {"start_cycle", "queued_running", "resume_waiting", "queued_paused", "resume_interrupted", "duplicate", "capacity_blocked"}
        if self.outcome not in allowed:
            raise ValueError("invalid admission outcome")
        if self.outcome == "capacity_blocked":
            if self.admission is not None or not self.retryable or not self.reason_code:
                raise ValueError("capacity_blocked must be retryable and have no admission")
        elif self.admission is None or self.retryable:
            raise ValueError("non-capacity outcome requires a non-retryable admission")
        return self


class CycleInboxItem(InputRuntimeModel):
    inbox_item_id: str = Field(default_factory=new_inbox_item_id)
    admission_id: str
    session_id: str
    cycle_id: str
    input_batch_id: str
    cycle_sequence: int = Field(ge=1)
    generation: int = Field(ge=0)
    state: InboxState = InboxState.QUEUED
    claim_token: str | None = None
    claim_expires_at: datetime | None = None
    attempt_count: int = Field(default=0, ge=0)
    last_error_code: str | None = None
    enqueued_at: datetime
    claimed_at: datetime | None = None
    applied_at: datetime | None = None
    cancelled_at: datetime | None = None

    @model_validator(mode="after")
    def validate_state(self) -> "CycleInboxItem":
        claim_fields = (self.claim_token, self.claim_expires_at, self.claimed_at)
        if self.state in {InboxState.CLAIMED, InboxState.APPLYING}:
            if not all(claim_fields):
                raise ValueError("claimed/applying item requires complete claim fields")
            if self.claim_expires_at <= self.claimed_at:
                raise ValueError("claim expiry must follow claim time")
        elif any(value is not None for value in claim_fields):
            raise ValueError("claim fields are only valid for claimed/applying items")
        if self.state == InboxState.APPLIED and self.applied_at is None:
            raise ValueError("applied inbox item requires applied_at")
        if self.state != InboxState.APPLIED and self.applied_at is not None:
            raise ValueError("applied_at is only valid for applied item")
        if self.state == InboxState.CANCELLED and self.cancelled_at is None:
            raise ValueError("cancelled item requires cancelled_at")
        if self.state != InboxState.CANCELLED and self.cancelled_at is not None:
            raise ValueError("cancelled_at is only valid for cancelled item")
        if self.state == InboxState.FAILED_TERMINAL and not self.last_error_code:
            raise ValueError("failed_terminal item requires last_error_code")
        return self


class ClaimedInboxRange(InputRuntimeModel):
    cycle_id: str
    generation: int = Field(ge=0)
    claim_token: str
    first_cycle_sequence: int = Field(ge=1)
    last_cycle_sequence: int = Field(ge=1)
    items: tuple[CycleInboxItem, ...]
    claimed_bytes: int = Field(default=0, ge=0)
    claim_expires_at: datetime

    @model_validator(mode="after")
    def validate_range(self) -> "ClaimedInboxRange":
        if not self.items or self.last_cycle_sequence < self.first_cycle_sequence:
            raise ValueError("claim must contain a non-empty ordered range")
        if [item.cycle_sequence for item in self.items] != list(range(self.first_cycle_sequence, self.last_cycle_sequence + 1)):
            raise ValueError("claim items must be contiguous")
        if any(item.state not in {InboxState.CLAIMED, InboxState.APPLYING} or item.cycle_id != self.cycle_id or item.generation != self.generation or item.claim_token != self.claim_token or item.claim_expires_at != self.claim_expires_at for item in self.items):
            raise ValueError("claim item identity/state mismatch")
        return self


class SessionControlCommand(InputRuntimeModel):
    control_id: str = Field(default_factory=new_control_id)
    session_id: str
    target_cycle_id: str | None = None
    generation: int = Field(ge=0)
    sequence_number: int = Field(ge=1)
    command: ControlCommandType
    state: ControlState = ControlState.QUEUED
    idempotency_key: str
    source_client_type: str
    source_message_ref: dict[str, Any] | None = None
    reason: str | None = None
    created_at: datetime
    acknowledged_at: datetime | None = None
    applied_at: datetime | None = None
    rejection_code: str | None = None

    @field_validator("source_message_ref")
    @classmethod
    def validate_source_ref(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        return cls.ensure_json_safe(value, "source_message_ref")

    @model_validator(mode="after")
    def validate_state(self) -> "SessionControlCommand":
        if self.command != ControlCommandType.RESET and self.target_cycle_id is None:
            raise ValueError("pause/continue require target_cycle_id")
        if self.state in {ControlState.ACKNOWLEDGED, ControlState.APPLIED} and self.acknowledged_at is None:
            raise ValueError("acknowledged/applied control requires acknowledged_at")
        if self.state not in {ControlState.ACKNOWLEDGED, ControlState.APPLIED} and self.acknowledged_at is not None:
            raise ValueError("acknowledged_at is only valid after acknowledgement")
        if self.state == ControlState.APPLIED and self.applied_at is None:
            raise ValueError("applied control requires applied_at")
        if self.state != ControlState.APPLIED and self.applied_at is not None:
            raise ValueError("applied_at is only valid for applied control")
        if self.state == ControlState.REJECTED and not self.rejection_code:
            raise ValueError("rejected control requires rejection_code")
        if self.state != ControlState.REJECTED and self.rejection_code is not None:
            raise ValueError("rejection_code is only valid for rejected control")
        return self


class ControlOutcome(InputRuntimeModel):
    outcome: ControlState | str
    command: SessionControlCommand
    effective_cycle_status: CycleStatus | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> "ControlOutcome":
        if self.outcome != "duplicate" and self.command.state != self.outcome:
            raise ValueError("control outcome must match command state")
        return self


class ActiveCycleSnapshot(InputRuntimeModel):
    cycle_id: str
    session_id: str
    generation: int = Field(ge=0)
    status: CycleStatus
    original_input_batch_id: str
    original_user_request: str
    messages_for_llm: list[dict[str, Any]] = Field(default_factory=list)
    cycle_trace: list[dict[str, Any]] = Field(default_factory=list)
    working_memory_ref: str | None = None
    applied_input_batch_ids: list[str] = Field(default_factory=list)
    applied_through_cycle_sequence: int = Field(default=0, ge=0)
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
    snapshot_revision: int = Field(default=1, ge=1)
    safe_checkpoint: CheckpointName
    created_at: datetime
    updated_at: datetime

    @field_validator("messages_for_llm", "cycle_trace")
    @classmethod
    def validate_json_collections(cls, value: Any, info: Any) -> Any:
        return cls.ensure_json_safe(value, info.field_name)

    @model_validator(mode="after")
    def validate_state(self) -> "ActiveCycleSnapshot":
        if len(set(self.applied_input_batch_ids)) != len(self.applied_input_batch_ids):
            raise ValueError("applied_input_batch_ids must be unique")
        if self.status == CycleStatus.WAITING_USER and not self.waiting_question:
            raise ValueError("waiting_user snapshot requires waiting_question")
        if self.status == CycleStatus.PAUSED_BY_USER and not self.pause_reason:
            raise ValueError("paused snapshot requires pause_reason")
        if self.status == CycleStatus.INTERRUPTED and not self.interruption_reason:
            raise ValueError("interrupted snapshot requires interruption_reason")
        if self.active_plan_id is None and any(value is not None for value in (self.active_plan_revision, self.active_plan_node_id)):
            raise ValueError("plan revision/node require active_plan_id")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


class CycleContextRevision(InputRuntimeModel):
    context_revision_id: str = Field(default_factory=new_context_revision_id)
    cycle_id: str
    session_id: str
    revision_number: int = Field(ge=1)
    parent_revision_ids: list[str] = Field(default_factory=list)
    reason: str
    applied_input_batch_ids: list[str] = Field(default_factory=list)
    applied_through_cycle_sequence: int = Field(default=0, ge=0)
    added_artifact_refs: list[str] = Field(default_factory=list)
    constraint_summary: str | None = None
    created_at: datetime

    @model_validator(mode="after")
    def validate_revision(self) -> "CycleContextRevision":
        if self.reason not in {"initial_input", "input_applied", "resumed", "recovered"}:
            raise ValueError("invalid context revision reason")
        if self.reason == "initial_input":
            if self.revision_number != 1 or self.parent_revision_ids:
                raise ValueError("initial revision must be revision 1 without parents")
        elif self.revision_number == 1 or len(self.parent_revision_ids) != 1:
            raise ValueError("v0.4 non-initial revision requires exactly one parent")
        if len(set(self.applied_input_batch_ids)) != len(self.applied_input_batch_ids):
            raise ValueError("applied_input_batch_ids must be unique")
        return self


class AgentEmission(InputRuntimeModel):
    emission_id: str = Field(default_factory=new_emission_id)
    session_id: str
    cycle_id: str
    context_revision_id: str
    kind: str
    text: str = Field(min_length=1)
    visibility: str = "user"
    importance: str = "normal"
    response_route: dict[str, Any]
    state: EmissionState = EmissionState.READY
    idempotency_key: str
    created_at: datetime
    delivered_at: datetime | None = None
    error_code: str | None = None

    @field_validator("response_route")
    @classmethod
    def validate_route(cls, value: dict[str, Any]) -> dict[str, Any]:
        return cls.ensure_json_safe(value, "response_route")

    @model_validator(mode="after")
    def validate_state(self) -> "AgentEmission":
        if self.kind not in {"intermediate", "runtime_notice", "question"} or self.visibility not in {"user", "debug", "internal"} or self.importance not in {"normal", "high"}:
            raise ValueError("invalid emission enum value")
        if self.state == EmissionState.DELIVERED and self.delivered_at is None:
            raise ValueError("delivered emission requires delivered_at")
        if self.state != EmissionState.DELIVERED and self.delivered_at is not None:
            raise ValueError("delivered_at is only valid for delivered emission")
        if self.state in {EmissionState.FAILED, EmissionState.UNKNOWN} and not self.error_code:
            raise ValueError("failed/unknown emission requires error_code")
        if self.state not in {EmissionState.FAILED, EmissionState.UNKNOWN} and self.error_code is not None:
            raise ValueError("error_code is only valid for failed/unknown emission")
        return self


class CycleFinalizationRecord(InputRuntimeModel):
    finalization_id: str = Field(default_factory=new_finalization_id)
    session_id: str
    cycle_id: str
    generation: int = Field(ge=0)
    context_revision_id: str
    expected_accepted_sequence: int = Field(ge=0)
    expected_applied_sequence: int = Field(ge=0)
    expected_control_sequence: int = Field(ge=0)
    state: FinalizationState = FinalizationState.PREPARED
    result_ref: str | None = None
    output_batch_id: str | None = None
    failure_code: str | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_state(self) -> "CycleFinalizationRecord":
        if self.expected_applied_sequence > self.expected_accepted_sequence:
            raise ValueError("expected applied sequence cannot exceed accepted sequence")
        if self.state == FinalizationState.PREPARED and self.expected_applied_sequence != self.expected_accepted_sequence:
            raise ValueError("prepared finalization requires equal input watermarks")
        if self.state in {FinalizationState.RESULT_PERSISTED, FinalizationState.OUTPUT_READY, FinalizationState.TERMINAL_COMMITTED} and not self.result_ref:
            raise ValueError("persisted finalization state requires result_ref")
        if self.state == FinalizationState.OUTPUT_READY and not self.output_batch_id:
            raise ValueError("output_ready requires output_batch_id")
        if self.state in {FinalizationState.FAILED_RECOVERABLE, FinalizationState.FAILED_TERMINAL} and not self.failure_code:
            raise ValueError("failed finalization requires failure_code")
        if self.state not in {FinalizationState.FAILED_RECOVERABLE, FinalizationState.FAILED_TERMINAL} and self.failure_code is not None:
            raise ValueError("failure_code is only valid for failed finalization")
        if self.state == FinalizationState.TERMINAL_COMMITTED and self.expected_applied_sequence != self.expected_accepted_sequence:
            raise ValueError("terminal finalization requires equal input watermarks")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


class CheckpointOutcome(InputRuntimeModel):
    checkpoint: CheckpointName
    action: CheckpointAction
    context_revision_id: str | None = None
    applied_through_cycle_sequence: int = Field(default=0, ge=0)
    applied_input_batch_ids: tuple[str, ...] = ()
    control_sequence: int = Field(default=0, ge=0)
    reason_code: str | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> "CheckpointOutcome":
        if self.action == CheckpointAction.INPUT_APPLIED:
            if self.context_revision_id is None or not self.applied_input_batch_ids:
                raise ValueError("input_applied requires revision and applied batch ids")
        elif self.applied_input_batch_ids:
            raise ValueError("applied batch ids are only valid for input_applied")
        if self.action in {CheckpointAction.PAUSE, CheckpointAction.WAIT, CheckpointAction.INTERRUPT, CheckpointAction.ABORT_FINALIZATION} and not self.reason_code:
            raise ValueError("non-continue checkpoint outcome requires reason_code")
        return self
