"""Validated domain records for the durable input runtime."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ID_PATTERNS = {
    "admission_id": re.compile(r"^adm_[0-9a-f]{32}$"),
    "inbox_item_id": re.compile(r"^inbx_[0-9a-f]{32}$"),
    "control_id": re.compile(r"^ctl_[0-9a-f]{32}$"),
    "context_revision_id": re.compile(r"^ctxrev_[0-9a-f]{32}$"),
    "emission_id": re.compile(r"^emit_[0-9a-f]{32}$"),
    "finalization_id": re.compile(r"^fin_[0-9a-f]{32}$"),
}
TERMINAL_SESSION_STATUSES = frozenset({"done", "error", "cancelled"})


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def new_admission_id() -> str:
    return _new_id("adm")


def new_inbox_item_id() -> str:
    return _new_id("inbx")


def new_control_id() -> str:
    return _new_id("ctl")


def new_context_revision_id() -> str:
    return _new_id("ctxrev")


def new_emission_id() -> str:
    return _new_id("emit")


def new_finalization_id() -> str:
    return _new_id("fin")


class InputRuntimeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    @field_validator("created_at", "updated_at", "admitted_at", "applied_at", "cancelled_at", "enqueued_at", "claimed_at", "acknowledged_at", "delivered_at", mode="before", check_fields=False)
    @classmethod
    def validate_timestamp(cls, value: Any) -> Any:
        if value is None:
            return value
        if not isinstance(value, datetime):
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("durable timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    @field_validator(*_ID_PATTERNS.keys(), check_fields=False)
    @classmethod
    def validate_stable_id(cls, value: str, info: Any) -> str:
        pattern = _ID_PATTERNS[info.field_name]
        if not pattern.fullmatch(value):
            raise ValueError(f"invalid stable {info.field_name}")
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
    cycle_status: Literal["idle", "running", "waiting_user", "pause_requested", "paused_by_user", "interrupted", "finalizing", "done", "error", "cancelled"] = "idle"
    accepted_through_session_sequence: int = Field(default=-1, ge=-1)
    active_cycle_accepted_through_sequence: int = Field(default=-1, ge=-1)
    active_cycle_applied_through_sequence: int = Field(default=-1, ge=-1)
    pending_control_sequence: int = Field(default=0, ge=0)
    applied_control_sequence: int = Field(default=0, ge=0)
    active_context_revision_id: str | None = None
    finalization_id: str | None = None
    revision: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_state(self) -> "SessionInputRuntimeState":
        if self.active_cycle_applied_through_sequence > self.active_cycle_accepted_through_sequence:
            raise ValueError("applied input watermark cannot exceed accepted watermark")
        if self.applied_control_sequence > self.pending_control_sequence:
            raise ValueError("applied control watermark cannot exceed pending watermark")
        if self.cycle_status == "idle" and self.active_cycle_id is not None:
            raise ValueError("idle session cannot have an active cycle")
        if self.cycle_status not in {"idle", "done", "error", "cancelled"} and self.active_cycle_id is None:
            raise ValueError("active cycle status requires active_cycle_id")
        if self.cycle_status == "finalizing" and self.finalization_id is None:
            raise ValueError("finalizing status requires finalization_id")
        if self.finalization_id is not None and self.cycle_status not in {"finalizing", "done", "error", "cancelled"}:
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
    session_sequence: int = Field(ge=0)
    target_cycle_id: str
    cycle_sequence: int = Field(ge=0)
    admitted_generation: int = Field(ge=0)
    admission_kind: Literal["start_cycle", "continue_running", "resume_waiting", "queue_paused", "resume_interrupted"]
    state: Literal["admitted", "applied", "cancelled", "failed_terminal"] = "admitted"
    idempotency_key: str
    admitted_at: datetime
    applied_at: datetime | None = None
    cancelled_at: datetime | None = None
    failure_code: str | None = None

    @model_validator(mode="after")
    def validate_state(self) -> "InputAdmissionRecord":
        if (self.admission_kind == "start_cycle") != (self.cycle_sequence == 0):
            raise ValueError("only start_cycle admission may use cycle_sequence=0")
        if self.state == "applied" and self.applied_at is None:
            raise ValueError("applied admission requires applied_at")
        if self.state != "applied" and self.applied_at is not None:
            raise ValueError("applied_at is only valid for applied admission")
        if self.state == "cancelled" and self.cancelled_at is None:
            raise ValueError("cancelled admission requires cancelled_at")
        if self.state != "cancelled" and self.cancelled_at is not None:
            raise ValueError("cancelled_at is only valid for cancelled admission")
        if self.state == "failed_terminal" and not self.failure_code:
            raise ValueError("failed_terminal admission requires failure_code")
        if self.state != "failed_terminal" and self.failure_code is not None:
            raise ValueError("failure_code is only valid for failed_terminal admission")
        return self


class InputAdmissionOutcome(InputRuntimeModel):
    outcome: Literal["start_cycle", "queued_running", "resume_waiting", "queued_paused", "resume_interrupted", "duplicate", "capacity_blocked"]
    admission: InputAdmissionRecord | None = None
    retryable: bool = False
    reason_code: str | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> "InputAdmissionOutcome":
        if self.outcome == "capacity_blocked":
            if self.admission is not None or not self.retryable or not self.reason_code:
                raise ValueError("capacity_blocked must be retryable and have no admission")
        elif self.admission is None:
            raise ValueError("successful admission outcome requires admission")
        return self


class CycleInboxItem(InputRuntimeModel):
    inbox_item_id: str = Field(default_factory=new_inbox_item_id)
    admission_id: str
    session_id: str
    cycle_id: str
    input_batch_id: str
    cycle_sequence: int = Field(ge=1)
    generation: int = Field(ge=0)
    state: Literal["queued", "claimed", "applying", "applied", "cancelled", "failed_terminal"] = "queued"
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
        claimed = self.state in {"claimed", "applying"}
        if claimed != bool(self.claim_token and self.claim_expires_at and self.claimed_at):
            raise ValueError("claimed/applying item requires complete claim fields")
        if self.state == "applied" and self.applied_at is None:
            raise ValueError("applied inbox item requires applied_at")
        if self.state != "applied" and self.applied_at is not None:
            raise ValueError("applied_at is only valid for applied item")
        if self.state == "cancelled" and self.cancelled_at is None:
            raise ValueError("cancelled item requires cancelled_at")
        if self.state != "cancelled" and self.cancelled_at is not None:
            raise ValueError("cancelled_at is only valid for cancelled item")
        if self.state == "failed_terminal" and not self.last_error_code:
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
        sequences = [item.cycle_sequence for item in self.items]
        if sequences != list(range(self.first_cycle_sequence, self.last_cycle_sequence + 1)):
            raise ValueError("claim items must be contiguous")
        if any(item.cycle_id != self.cycle_id or item.generation != self.generation or item.claim_token != self.claim_token for item in self.items):
            raise ValueError("claim item identity mismatch")
        return self


class SessionControlCommand(InputRuntimeModel):
    control_id: str = Field(default_factory=new_control_id)
    session_id: str
    target_cycle_id: str | None = None
    generation: int = Field(ge=0)
    sequence_number: int = Field(ge=1)
    command: Literal["pause", "continue", "reset"]
    state: Literal["queued", "acknowledged", "applied", "rejected", "cancelled"] = "queued"
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
        if self.command != "reset" and self.target_cycle_id is None:
            raise ValueError("pause/continue require target_cycle_id")
        if self.state in {"acknowledged", "applied"} and self.acknowledged_at is None:
            raise ValueError("acknowledged/applied control requires acknowledged_at")
        if self.state == "applied" and self.applied_at is None:
            raise ValueError("applied control requires applied_at")
        if self.state != "applied" and self.applied_at is not None:
            raise ValueError("applied_at is only valid for applied control")
        if self.state == "rejected" and not self.rejection_code:
            raise ValueError("rejected control requires rejection_code")
        if self.state != "rejected" and self.rejection_code is not None:
            raise ValueError("rejection_code is only valid for rejected control")
        return self


class ControlOutcome(InputRuntimeModel):
    outcome: Literal["queued", "acknowledged", "applied", "rejected", "duplicate"]
    command: SessionControlCommand
    effective_cycle_status: str | None = None


class ActiveCycleSnapshot(InputRuntimeModel):
    cycle_id: str
    session_id: str
    generation: int = Field(ge=0)
    status: str
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
    active_plan_revision: int | None = Field(default=None, ge=0)
    active_plan_node_id: str | None = None
    artifact_refs: list[str] = Field(default_factory=list)
    read_artifact_refs: list[str] = Field(default_factory=list)
    result_refs: list[str] = Field(default_factory=list)
    config_revision: str | None = None
    snapshot_revision: int = Field(ge=0)
    safe_checkpoint: str
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
        if self.status == "waiting_user" and not self.waiting_question:
            raise ValueError("waiting_user snapshot requires waiting_question")
        if self.status == "paused_by_user" and not self.pause_reason:
            raise ValueError("paused snapshot requires pause_reason")
        if self.status == "interrupted" and not self.interruption_reason:
            raise ValueError("interrupted snapshot requires interruption_reason")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


class CycleContextRevision(InputRuntimeModel):
    context_revision_id: str = Field(default_factory=new_context_revision_id)
    cycle_id: str
    session_id: str
    revision_number: int = Field(ge=1)
    parent_revision_ids: list[str] = Field(default_factory=list)
    reason: Literal["initial_input", "input_applied", "resumed", "recovered"]
    applied_input_batch_ids: list[str] = Field(default_factory=list)
    applied_through_cycle_sequence: int = Field(ge=0)
    added_artifact_refs: list[str] = Field(default_factory=list)
    constraint_summary: str | None = None
    created_at: datetime

    @model_validator(mode="after")
    def validate_revision(self) -> "CycleContextRevision":
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
    kind: Literal["intermediate", "runtime_notice", "question"]
    text: str = Field(min_length=1)
    visibility: Literal["user", "debug", "internal"] = "user"
    importance: Literal["normal", "high"] = "normal"
    response_route: dict[str, Any]
    state: Literal["ready", "delivering", "delivered", "failed", "unknown", "cancelled"] = "ready"
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
        if self.state == "delivered" and self.delivered_at is None:
            raise ValueError("delivered emission requires delivered_at")
        if self.state != "delivered" and self.delivered_at is not None:
            raise ValueError("delivered_at is only valid for delivered emission")
        if self.state in {"failed", "unknown"} and not self.error_code:
            raise ValueError("failed/unknown emission requires error_code")
        if self.state not in {"failed", "unknown"} and self.error_code is not None:
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
    state: Literal["prepared", "aborted_new_input", "aborted_control", "result_persisted", "output_ready", "terminal_committed", "failed_recoverable", "failed_terminal"] = "prepared"
    result_ref: str | None = None
    output_batch_id: str | None = None
    failure_code: str | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_state(self) -> "CycleFinalizationRecord":
        if self.expected_applied_sequence > self.expected_accepted_sequence:
            raise ValueError("expected applied sequence cannot exceed accepted sequence")
        if self.state in {"result_persisted", "output_ready", "terminal_committed"} and not self.result_ref:
            raise ValueError("persisted finalization state requires result_ref")
        if self.state in {"output_ready", "terminal_committed"} and not self.output_batch_id:
            raise ValueError("output state requires output_batch_id")
        if self.state in {"failed_recoverable", "failed_terminal"} and not self.failure_code:
            raise ValueError("failed finalization requires failure_code")
        if self.state not in {"failed_recoverable", "failed_terminal"} and self.failure_code is not None:
            raise ValueError("failure_code is only valid for failed finalization")
        if self.state == "terminal_committed" and self.expected_applied_sequence != self.expected_accepted_sequence:
            raise ValueError("terminal finalization requires equal input watermarks")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


class CheckpointOutcome(InputRuntimeModel):
    checkpoint: str
    action: Literal["continue", "input_applied", "pause", "wait", "interrupt", "abort_finalization"]
    context_revision_id: str | None = None
    applied_through_cycle_sequence: int = Field(default=0, ge=0)
    applied_input_batch_ids: tuple[str, ...] = ()
    control_sequence: int = Field(default=0, ge=0)
    reason_code: str | None = None
