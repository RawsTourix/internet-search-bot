"""Storage-neutral runtime handoff domain records."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, field_validator, model_validator


class RuntimeHandoffState(str, Enum):
    HANDED_OFF = "handed_off"
    COMPLETED = "completed"
    AMBIGUOUS = "ambiguous"


class RuntimeHandoffRecord(BaseModel):
    """Durable evidence that execution crossed into the side-effecting runtime."""

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    admission_id: str
    session_id: str
    input_batch_id: str
    cycle_id: str
    handoff_token: str
    state: RuntimeHandoffState = RuntimeHandoffState.HANDED_OFF
    handed_off_at: datetime
    completed_at: datetime | None = None
    ambiguous_at: datetime | None = None
    error_code: str | None = None

    @field_validator(
        "admission_id",
        "session_id",
        "input_batch_id",
        "cycle_id",
        "handoff_token",
        mode="before",
    )
    @classmethod
    def normalize_identity(cls, value: str) -> str:
        normalized = str(value).strip()
        if not normalized:
            raise ValueError("handoff identity must not be empty")
        return normalized

    @field_validator(
        "handed_off_at",
        "completed_at",
        "ambiguous_at",
        mode="before",
    )
    @classmethod
    def normalize_timestamp(
        cls,
        value: datetime | str | None,
    ) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as error:
                raise ValueError("invalid handoff timestamp") from error
        if not isinstance(value, datetime):
            raise ValueError("invalid handoff timestamp type")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("handoff timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_record(self) -> "RuntimeHandoffRecord":
        if self.state == RuntimeHandoffState.HANDED_OFF:
            if self.completed_at is not None or self.ambiguous_at is not None:
                raise ValueError("open handoff cannot have terminal timestamps")
            if self.error_code is not None:
                raise ValueError("open handoff cannot have error_code")
        elif self.state == RuntimeHandoffState.COMPLETED:
            if self.completed_at is None or self.ambiguous_at is not None:
                raise ValueError("completed handoff timestamp mismatch")
            if self.error_code is not None:
                raise ValueError("completed handoff cannot have error_code")
            if self.completed_at < self.handed_off_at:
                raise ValueError("completed_at cannot precede handed_off_at")
        elif self.state == RuntimeHandoffState.AMBIGUOUS:
            if self.ambiguous_at is None or self.completed_at is not None:
                raise ValueError("ambiguous handoff timestamp mismatch")
            if not self.error_code:
                raise ValueError("ambiguous handoff requires error_code")
            if self.ambiguous_at < self.handed_off_at:
                raise ValueError("ambiguous_at cannot precede handed_off_at")
        return self
