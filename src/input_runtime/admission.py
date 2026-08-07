"""Application-facing admission contracts for committed input batches."""

from __future__ import annotations

from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import AdmissionKind, AdmissionState, InputAdmissionRecord


class InputAdmissionAction(str, Enum):
    START_CYCLE = "start_cycle"
    QUEUED_RUNNING = "queued_running"
    RESUME_WAITING = "resume_waiting"
    QUEUED_PAUSED = "queued_paused"
    RESUME_INTERRUPTED = "resume_interrupted"
    DUPLICATE = "duplicate"
    CAPACITY_BLOCKED = "capacity_blocked"


class InputAdmissionOutcome(BaseModel):
    """Structured, transport-neutral result of one durable admission attempt."""

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    input_batch_id: str
    session_id: str
    admission_id: str | None = None
    target_cycle_id: str | None = None
    session_sequence: int | None = Field(default=None, ge=1)
    cycle_sequence: int | None = Field(default=None, ge=0)
    action: InputAdmissionAction
    admission_kind: AdmissionKind | None = None
    admission_state: AdmissionState | None = None
    should_start_runner: bool = False
    should_wake_runner: bool = False
    user_projection_key: str
    retryable: bool = False
    reason_code: str
    admission: InputAdmissionRecord | None = None

    @property
    def admitted_generation(self) -> int | None:
        """Generation accepted by durable admission authority, when accepted."""
        return (
            self.admission.admitted_generation
            if self.admission is not None
            else None
        )

    @model_validator(mode="after")
    def validate_relation(self) -> "InputAdmissionOutcome":
        if self.action == InputAdmissionAction.CAPACITY_BLOCKED:
            if self.admission is not None or self.admission_id is not None:
                raise ValueError("capacity outcome cannot contain admission")
            if not self.retryable:
                raise ValueError("capacity outcome must be retryable")
            return self

        if self.admission is None:
            raise ValueError("accepted outcome requires admission")
        expected = self.admission
        relation = (
            self.input_batch_id,
            self.session_id,
            self.admission_id,
            self.target_cycle_id,
            self.session_sequence,
            self.cycle_sequence,
            self.admission_kind,
            self.admission_state,
        )
        authoritative = (
            expected.input_batch_id,
            expected.session_id,
            expected.admission_id,
            expected.target_cycle_id,
            expected.session_sequence,
            expected.cycle_sequence,
            expected.admission_kind,
            expected.state,
        )
        if relation != authoritative:
            raise ValueError("outcome/admission relation mismatch")
        if self.retryable:
            raise ValueError("accepted outcome cannot be retryable")
        if self.action == InputAdmissionAction.START_CYCLE:
            if not self.should_start_runner or self.cycle_sequence != 0:
                raise ValueError("start_cycle outcome requires initial runner")
        if self.action == InputAdmissionAction.QUEUED_PAUSED:
            if self.should_start_runner or self.should_wake_runner:
                raise ValueError("paused admission cannot start or wake runner")
        return self

    @classmethod
    def accepted(
        cls,
        *,
        admission: InputAdmissionRecord,
        action: InputAdmissionAction,
        should_start_runner: bool,
        should_wake_runner: bool,
        user_projection_key: str,
        reason_code: str,
    ) -> "InputAdmissionOutcome":
        return cls(
            input_batch_id=admission.input_batch_id,
            session_id=admission.session_id,
            admission_id=admission.admission_id,
            target_cycle_id=admission.target_cycle_id,
            session_sequence=admission.session_sequence,
            cycle_sequence=admission.cycle_sequence,
            action=action,
            admission_kind=admission.admission_kind,
            admission_state=admission.state,
            should_start_runner=should_start_runner,
            should_wake_runner=should_wake_runner,
            user_projection_key=user_projection_key,
            retryable=False,
            reason_code=reason_code,
            admission=admission,
        )


@runtime_checkable
class CommittedInputBatchReader(Protocol):
    async def get_committed(self, input_batch_id: str) -> Any: ...


@runtime_checkable
class AdmissionWakeCoordinator(Protocol):
    async def wake(self, session_id: str, *, cycle_id: str) -> bool: ...
