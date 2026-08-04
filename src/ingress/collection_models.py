"""Transport-neutral models for explicit user-controlled input collection."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..core.models import ClientType
from .models import (
    ClientConversationRef,
    ClientResponseRoute,
    CommittedInputBatch,
    InputBatchDraftState,
    is_input_batch_id,
)


_COLLECTION_ID_RE = re.compile(r"^icol_[0-9a-f]{32}$")


def new_input_collection_id() -> str:
    return f"icol_{uuid4().hex}"


def is_input_collection_id(value: str) -> bool:
    return bool(_COLLECTION_ID_RE.fullmatch(value))


def _required(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


class _CollectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InputAssemblyMode(str, Enum):
    AUTO = "auto"
    EXPLICIT = "explicit"


class InputCommitPolicy(str, Enum):
    AUTOMATIC = "automatic"
    EXPLICIT = "explicit"


class InputCollectionState(str, Enum):
    COLLECTING = "collecting"
    COMMIT_REQUESTED = "commit_requested"
    COMMITTED = "committed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"
    FAILED = "failed"


_ACTIVE_COLLECTION_STATES = {
    InputCollectionState.COLLECTING,
    InputCollectionState.COMMIT_REQUESTED,
}


class InputDraftScope(_CollectionModel):
    """Exact authority boundary for one user-controlled collection."""

    session_id: str
    client_type: ClientType
    client_instance_id: str
    conversation: ClientConversationRef
    principal_id: str

    @field_validator("session_id", "client_instance_id", "principal_id")
    @classmethod
    def validate_required(cls, value: str, info) -> str:
        return _required(value, info.field_name)

    def canonical_payload(self) -> dict[str, str | None]:
        return {
            "session_id": self.session_id,
            "client_type": self.client_type.value,
            "client_instance_id": self.client_instance_id,
            "conversation_id": self.conversation.conversation_id,
            "thread_id": self.conversation.thread_id,
            "principal_id": self.principal_id,
        }


class InputCollectionRecord(_CollectionModel):
    """Durable collection session that may exist before the first input event."""

    schema_version: Literal[1] = 1
    collection_id: str
    scope: InputDraftScope
    assembly_mode: InputAssemblyMode = InputAssemblyMode.EXPLICIT
    commit_policy: InputCommitPolicy = InputCommitPolicy.EXPLICIT
    state: InputCollectionState = InputCollectionState.COLLECTING
    bound_input_batch_id: str | None = None
    response_route: ClientResponseRoute
    locale: str | None = None
    opened_at: datetime
    updated_at: datetime
    commit_requested_at: datetime | None = None
    terminal_at: datetime | None = None
    failure_code: str | None = None

    @field_validator("collection_id")
    @classmethod
    def validate_collection_id(cls, value: str) -> str:
        if not is_input_collection_id(value):
            raise ValueError("invalid input collection ID")
        return value

    @field_validator("bound_input_batch_id")
    @classmethod
    def validate_batch_id(cls, value: str | None) -> str | None:
        if value is not None and not is_input_batch_id(value):
            raise ValueError("invalid bound input batch ID")
        return value

    @field_validator("locale", "failure_code")
    @classmethod
    def normalize_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator(
        "opened_at",
        "updated_at",
        "commit_requested_at",
        "terminal_at",
    )
    @classmethod
    def validate_timestamp(cls, value: datetime | None) -> datetime | None:
        return _utc(value) if value is not None else None

    @model_validator(mode="after")
    def validate_state_metadata(self) -> "InputCollectionRecord":
        if self.assembly_mode != InputAssemblyMode.EXPLICIT:
            raise ValueError("collection record must use explicit assembly mode")
        if self.commit_policy != InputCommitPolicy.EXPLICIT:
            raise ValueError("collection record must use explicit commit policy")
        if self.state == InputCollectionState.COMMIT_REQUESTED:
            if self.commit_requested_at is None:
                raise ValueError("commit_requested state requires timestamp")
        if self.state in _ACTIVE_COLLECTION_STATES:
            if self.terminal_at is not None:
                raise ValueError("active collection must not have terminal timestamp")
        elif self.terminal_at is None:
            raise ValueError("terminal collection requires terminal timestamp")
        return self

    @property
    def is_active(self) -> bool:
        return self.state in _ACTIVE_COLLECTION_STATES


class InputDraftControlAction(str, Enum):
    START = "start"
    INSPECT = "inspect"
    COMMIT = "commit"
    CANCEL = "cancel"
    BIND = "bind"


class InputDraftControlStatus(str, Enum):
    STARTED = "started"
    PROMOTED_AUTO_DRAFT = "promoted_auto_draft"
    ALREADY_ACTIVE = "already_active"
    INSPECTED = "inspected"
    EMPTY = "empty"
    COMMIT_REQUESTED = "commit_requested"
    COMMITTED = "committed"
    CANCELLED = "cancelled"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    FAILED = "failed"


class InputDraftControlResult(_CollectionModel):
    action: InputDraftControlAction
    status: InputDraftControlStatus
    duplicate: bool = False
    collection: InputCollectionRecord | None = None
    input_batch_id: str | None = None
    draft_state: InputBatchDraftState | None = None
    file_count: int = Field(default=0, ge=0)
    text_part_count: int = Field(default=0, ge=0)
    semantic_part_count: int = Field(default=0, ge=0)
    committed_batch: CommittedInputBatch | None = None
    error_code: str | None = None

    @field_validator("input_batch_id")
    @classmethod
    def validate_result_batch_id(cls, value: str | None) -> str | None:
        if value is not None and not is_input_batch_id(value):
            raise ValueError("invalid result input batch ID")
        return value

    @field_validator("error_code")
    @classmethod
    def normalize_error_code(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class InputDraftControlConflictError(RuntimeError):
    """A control idempotency key or exact scope was reused inconsistently."""
