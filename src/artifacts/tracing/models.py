"""Structured append-only events for artifact lifecycle diagnostics."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


ArtifactTraceDirection = Literal["inbound", "internal", "outbound"]
ArtifactTraceStatus = Literal[
    "started",
    "succeeded",
    "partially_succeeded",
    "failed",
    "unknown",
    "observed",
]


def new_artifact_trace_event_id() -> str:
    return f"aevt_{uuid4().hex}"


def _normalize_optional_identifier(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


class ArtifactTraceCorrelation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ingress_event_id: str | None = None
    input_batch_id: str | None = None
    output_batch_id: str | None = None
    delivery_id: str | None = None
    candidate_id: str | None = None

    @field_validator(
        "ingress_event_id",
        "input_batch_id",
        "output_batch_id",
        "delivery_id",
        "candidate_id",
        mode="before",
    )
    @classmethod
    def normalize_identifiers(cls, value: Any) -> str | None:
        return _normalize_optional_identifier(value)


class ArtifactTraceTransport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_type: str | None = None
    client_instance_id: str | None = None
    conversation_id: str | None = None
    thread_id: str | None = None
    source_update_id: str | None = None
    source_message_id: str | None = None
    source_group_id: str | None = None
    delivery_mode: str | None = None
    client_message_id: str | None = None

    @field_validator(
        "client_type",
        "client_instance_id",
        "conversation_id",
        "thread_id",
        "source_update_id",
        "source_message_id",
        "source_group_id",
        "delivery_mode",
        "client_message_id",
        mode="before",
    )
    @classmethod
    def normalize_identifiers(cls, value: Any) -> str | None:
        return _normalize_optional_identifier(value)


class ArtifactTraceArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str | None = None
    artifact_lineage_id: str | None = None
    content_id: str | None = None
    filename: str | None = None
    format_id: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    content_hash: str | None = None
    purpose: str | None = None
    version: int | None = Field(default=None, ge=1)


class ArtifactTraceError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error_type: str
    error_code: str | None = None
    message: str | None = None
    retryable: bool | None = None


class ArtifactTraceEvent(BaseModel):
    """One immutable diagnostic fact about an artifact lifecycle transition."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    trace_type: Literal["artifact_event"] = "artifact_event"
    event_id: str = Field(default_factory=new_artifact_trace_event_id)
    occurred_at: datetime

    session_id: str
    cycle_id: str | None = None
    operation_id: str | None = None

    event_type: str
    stage: str
    status: ArtifactTraceStatus
    direction: ArtifactTraceDirection = "internal"

    correlation: ArtifactTraceCorrelation = Field(
        default_factory=ArtifactTraceCorrelation
    )
    transport: ArtifactTraceTransport | None = None
    artifact: ArtifactTraceArtifact | None = None
    metrics: dict[str, int | float | str | bool | None] = Field(
        default_factory=dict
    )
    error: ArtifactTraceError | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator(
        "session_id",
        "event_type",
        "stage",
    )
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("artifact trace text field must not be empty")
        return normalized

    @field_validator(
        "cycle_id",
        "operation_id",
    )
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("occurred_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("artifact trace timestamp must be timezone-aware")
        return value.astimezone(timezone.utc)
