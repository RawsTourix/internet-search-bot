"""Deterministic response-anchor selection independent of response routing."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .ids import is_interaction_id, new_response_anchor_id


class FrozenAnchorMetadata(dict[str, Any]):
    def _blocked(self, *args: Any, **kwargs: Any) -> None:
        raise TypeError("response anchor metadata is immutable")

    __setitem__ = _blocked
    __delitem__ = _blocked
    clear = _blocked
    pop = _blocked
    popitem = _blocked
    setdefault = _blocked
    update = _blocked


class ClientResponseAnchorKind(str, Enum):
    EXPLICIT = "explicit"
    INSTRUCTION = "instruction"
    CAPTION = "caption"
    ATTACHMENT = "attachment"
    FALLBACK = "fallback"


class _AnchorModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClientResponseAnchorCandidate(_AnchorModel):
    client_message_id: str
    source_event_id: str | None = None
    source_message_id: str | None = None
    kind: ClientResponseAnchorKind
    priority: int = Field(ge=0, le=10_000)
    occurred_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("client_message_id")
    @classmethod
    def validate_message_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("client_message_id must not be empty")
        return normalized

    @field_validator("occurred_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("anchor candidate timestamp must be timezone-aware")
        return value.astimezone(timezone.utc)


class ClientResponseAnchor(_AnchorModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    anchor_id: str
    client_message_id: str
    source_event_id: str | None = None
    source_message_id: str | None = None
    kind: ClientResponseAnchorKind
    priority: int = Field(ge=0, le=10_000)
    occurred_at: datetime
    selected_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("anchor_id")
    @classmethod
    def validate_anchor_id(cls, value: str) -> str:
        if not is_interaction_id(value, prefix="anch"):
            raise ValueError("invalid anchor_id")
        return value

    @field_validator("client_message_id")
    @classmethod
    def validate_message_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("client_message_id must not be empty")
        return normalized

    @field_validator("occurred_at", "selected_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("anchor timestamp must be timezone-aware")
        return value.astimezone(timezone.utc)

    @field_validator("metadata")
    @classmethod
    def freeze_metadata(cls, value: dict[str, Any]) -> FrozenAnchorMetadata:
        return FrozenAnchorMetadata(value)

    @classmethod
    def from_candidate(
        cls,
        candidate: ClientResponseAnchorCandidate,
        *,
        selected_at: datetime | None = None,
    ) -> "ClientResponseAnchor":
        now = selected_at or datetime.now(timezone.utc)
        return cls(
            anchor_id=new_response_anchor_id(),
            client_message_id=candidate.client_message_id,
            source_event_id=candidate.source_event_id,
            source_message_id=candidate.source_message_id,
            kind=candidate.kind,
            priority=candidate.priority,
            occurred_at=candidate.occurred_at,
            selected_at=now,
            metadata=dict(candidate.metadata),
        )


class ResponseAnchorSelector:
    """Select the highest-priority, latest and stably-tied candidate."""

    _PRIORITIES = {
        ClientResponseAnchorKind.EXPLICIT: 500,
        ClientResponseAnchorKind.INSTRUCTION: 400,
        ClientResponseAnchorKind.CAPTION: 300,
        ClientResponseAnchorKind.ATTACHMENT: 200,
        ClientResponseAnchorKind.FALLBACK: 100,
    }

    @classmethod
    def priority_for(cls, kind: ClientResponseAnchorKind) -> int:
        return cls._PRIORITIES[kind]

    def select(
        self,
        candidates: list[ClientResponseAnchorCandidate],
        *,
        selected_at: datetime | None = None,
    ) -> ClientResponseAnchor:
        if not candidates:
            raise ValueError("response anchor selection requires a candidate")
        winner = max(candidates, key=self.candidate_sort_key)
        return ClientResponseAnchor.from_candidate(
            winner,
            selected_at=selected_at,
        )

    def select_with_current(
        self,
        current: ClientResponseAnchor | None,
        candidate: ClientResponseAnchorCandidate,
        *,
        selected_at: datetime | None = None,
    ) -> ClientResponseAnchor:
        if current is None:
            return self.select([candidate], selected_at=selected_at)
        current_candidate = ClientResponseAnchorCandidate(
            client_message_id=current.client_message_id,
            source_event_id=current.source_event_id,
            source_message_id=current.source_message_id,
            kind=current.kind,
            priority=current.priority,
            occurred_at=current.occurred_at,
            metadata=dict(current.metadata),
        )
        if self.candidate_sort_key(candidate) <= self.candidate_sort_key(
            current_candidate
        ):
            return current
        return ClientResponseAnchor.from_candidate(
            candidate,
            selected_at=selected_at,
        )

    @staticmethod
    def candidate_sort_key(
        candidate: ClientResponseAnchorCandidate,
    ) -> tuple[int, datetime, str, str]:
        return (
            candidate.priority,
            candidate.occurred_at,
            candidate.source_message_id or "",
            candidate.client_message_id,
        )
