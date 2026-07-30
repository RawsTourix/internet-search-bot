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
    RELOCATE = "relocate"


class PresentationDeletionState(str, Enum):
    NOT_REQUESTED = "not_requested"
    DELETED = "deleted"
    FAILED = "failed"
    UNKNOWN = "unknown"


class SupersededPresentationHandle(BaseModel):
    """One immutable historical handle from an older presentation generation."""

    model_config = ConfigDict(extra="forbid")

    client_message_id: str
    generation: int = Field(ge=1)
    superseded_at: datetime
    deletion_state: PresentationDeletionState = (
        PresentationDeletionState.NOT_REQUESTED
    )

    @field_validator("client_message_id")
    @classmethod
    def validate_client_message_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("client_message_id must not be empty")
        return normalized

    @field_validator("superseded_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("superseded_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class InputBatchPresentationRef(BaseModel):
    """One durable presentation binding for an input batch/client binding.

    ``client_message_id`` remains the serialized compatibility name for the
    active handle. ``presentation_generation`` and ``superseded_handles`` make
    the writable generation explicit and prevent restart recovery from editing
    an older Telegram message after relocation.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    input_batch_id: str
    client_binding_id: str
    presentation_id: str
    token_hash: str
    client_message_id: str | None = None
    presentation_generation: int = Field(default=0, ge=0)
    anchor_source_message_id: str | None = None
    superseded_handles: list[SupersededPresentationHandle] = Field(
        default_factory=list
    )
    pending_relocation_token_hash: str | None = None
    pending_relocation_generation: int | None = Field(default=None, ge=1)
    pending_anchor_source_message_id: str | None = None
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

    @property
    def active_client_message_id(self) -> str | None:
        return self.client_message_id

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

    @field_validator(
        "client_message_id",
        "anchor_source_message_id",
        "pending_relocation_token_hash",
        "pending_anchor_source_message_id",
    )
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

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
        if self.state == PresentationState.BOUND:
            if not self.client_message_id:
                raise ValueError("bound presentation requires client_message_id")
            if self.presentation_generation < 1:
                raise ValueError("bound presentation requires generation >= 1")
        if self.state == PresentationState.RESERVED and self.presentation_generation != 0:
            raise ValueError("reserved presentation must use generation 0")
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

        pending = (
            self.pending_relocation_token_hash,
            self.pending_relocation_generation,
            self.pending_anchor_source_message_id,
        )
        if any(value is not None for value in pending):
            if not all(value is not None for value in pending):
                raise ValueError("pending relocation fields must be set together")
            if self.state != PresentationState.BOUND:
                raise ValueError("only a bound presentation can relocate")
            if self.pending_relocation_generation != self.presentation_generation + 1:
                raise ValueError("pending relocation generation must be current + 1")

        generations = [item.generation for item in self.superseded_handles]
        if len(generations) != len(set(generations)):
            raise ValueError("superseded presentation generations must be unique")
        if any(item.generation >= self.presentation_generation for item in self.superseded_handles):
            raise ValueError("superseded generation must be older than active generation")
        if self.client_message_id and any(
            item.client_message_id == self.client_message_id
            for item in self.superseded_handles
        ):
            raise ValueError("active presentation handle cannot be superseded")
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
            anchor_source_message_id=(
                response_anchor.client_message_id
                if response_anchor is not None
                else None
            ),
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
    """Safe client projection; token is returned only for create/relocate."""

    model_config = ConfigDict(extra="forbid")

    presentation_id: str
    presentation_token: str | None = None
    client_message_id: str | None = None
    active_client_message_id: str | None = None
    state: PresentationState
    presentation_generation: int = Field(default=0, ge=0)
    relocation_generation: int | None = Field(default=None, ge=1)
    previous_client_message_id: str | None = None

    @model_validator(mode="after")
    def validate_active_alias(self) -> "PublicPresentationRef":
        if (
            self.client_message_id is not None
            and self.active_client_message_id is not None
            and self.client_message_id != self.active_client_message_id
        ):
            raise ValueError("active presentation message aliases disagree")
        return self
