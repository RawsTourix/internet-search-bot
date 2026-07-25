"""Durable, structured presentation state for an open input batch."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..localization.models import LocalizationMessage
from .anchors import ClientResponseAnchor
from .ids import is_interaction_id, new_presentation_id


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def hash_presentation_token(token: str) -> str:
    """Hash a public presentation token; the plaintext is never persisted."""
    return f"sha256:{hashlib.sha256(token.encode('utf-8')).hexdigest()}"


class PresentationState(str, Enum):
    RESERVED = "reserved"
    BOUND = "bound"
    CLOSED = "closed"
    FAILED = "failed"
    EXPIRED = "expired"


class PresentationAckPolicy(str, Enum):
    CREATE = "create"
    UPDATE_EXISTING = "update_existing"
    SILENT = "silent"
    THROTTLED_UPDATE = "throttled_update"


class InputBatchPresentationRef(BaseModel):
    """One durable presentation binding for an input batch/client binding."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    input_batch_id: str
    client_binding_id: str
    presentation_id: str
    token_hash: str
    client_message_id: str | None = None
    state: PresentationState = PresentationState.RESERVED
    pending_terminal_state: PresentationState | None = None
    message: LocalizationMessage
    locale: str
    file_count: int = Field(default=0, ge=0)
    text_part_count: int = Field(default=0, ge=0)
    update_count: int = Field(default=0, ge=0)
    response_anchor: ClientResponseAnchor | None = None
    created_at: datetime
    updated_at: datetime
    closed_at: datetime | None = None
    error_code: str | None = None

    @field_validator("presentation_id")
    @classmethod
    def validate_presentation_id(cls, value: str) -> str:
        if not is_interaction_id(value, prefix="iprs"):
            raise ValueError("invalid presentation_id")
        return value

    @field_validator(
        "input_batch_id",
        "client_binding_id",
        "token_hash",
        "locale",
    )
    @classmethod
    def validate_required(cls, value: str, info) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{info.field_name} must not be empty")
        return normalized

    @field_validator("created_at", "updated_at", "closed_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("presentation timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "InputBatchPresentationRef":
        terminal = {
            PresentationState.CLOSED,
            PresentationState.FAILED,
            PresentationState.EXPIRED,
        }
        if self.state in terminal and self.closed_at is None:
            raise ValueError("terminal presentation state requires closed_at")
        if self.state == PresentationState.BOUND and not self.client_message_id:
            raise ValueError("bound presentation requires client_message_id")
        if self.pending_terminal_state is not None:
            if self.pending_terminal_state not in {
                PresentationState.CLOSED,
                PresentationState.FAILED,
            }:
                raise ValueError("invalid pending presentation terminal state")
            if self.state != PresentationState.RESERVED:
                raise ValueError(
                    "pending terminal state is only valid before transport binding"
                )
        return self

    @classmethod
    def reserve(
        cls,
        *,
        input_batch_id: str,
        client_binding_id: str,
        token: str,
        message: LocalizationMessage,
        locale: str,
        file_count: int = 0,
        text_part_count: int = 0,
        response_anchor: ClientResponseAnchor | None = None,
        now: datetime | None = None,
    ) -> "InputBatchPresentationRef":
        timestamp = now or utc_now()
        return cls(
            input_batch_id=input_batch_id,
            client_binding_id=client_binding_id,
            presentation_id=new_presentation_id(),
            token_hash=hash_presentation_token(token),
            message=message,
            locale=locale,
            file_count=file_count,
            text_part_count=text_part_count,
            response_anchor=response_anchor,
            created_at=timestamp,
            updated_at=timestamp,
        )


class InputPresentationEvent(BaseModel):
    """Transport-neutral callback payload returned by ingress."""

    model_config = ConfigDict(extra="forbid")

    message_key: str
    severity: Literal["info", "warning", "error"] = "info"
    params: dict[str, Any] = Field(default_factory=dict)
    locale: str


class PublicPresentationRef(BaseModel):
    """Safe reference returned to a client; contains no stored token hash."""

    model_config = ConfigDict(extra="forbid")

    presentation_id: str
    presentation_token: str | None = None
    client_message_id: str | None = None
    state: PresentationState
