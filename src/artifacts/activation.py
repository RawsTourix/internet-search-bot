"""Bounded runtime provenance for exact artifact activation."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, field_validator

from .models import is_artifact_id


class ArtifactActivationReason(str, Enum):
    CURRENT_INPUT_BATCH = "current_input_batch"
    EXPLICIT_REFERENCE = "explicit_reference"
    CREATED_IN_CYCLE = "created_in_cycle"
    MODIFIED_IN_CYCLE = "modified_in_cycle"
    CATALOG_RESULT = "catalog_result"
    SEARCH_RESULT = "search_result"
    CLIENT_SELECTION = "client_selection"


class ArtifactCatalogScope(str, Enum):
    CURRENT = "current"
    SESSION = "session"
    WORKSPACE = "workspace"


class ArtifactActivation(BaseModel):
    """One exact version activated for the current AgentCycle."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    cycle_id: str
    reason: ArtifactActivationReason
    scope: ArtifactCatalogScope = ArtifactCatalogScope.CURRENT
    source_operation_id: str | None = None
    activated_at: datetime

    @field_validator("artifact_id")
    @classmethod
    def validate_artifact_id(cls, value: str) -> str:
        if not is_artifact_id(value):
            raise ValueError("invalid activated artifact_id")
        return value

    @field_validator("cycle_id")
    @classmethod
    def validate_cycle_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("cycle_id must not be empty")
        return normalized

    @field_validator("source_operation_id")
    @classmethod
    def normalize_operation_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("activated_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("activated_at must be timezone-aware")
        return value.astimezone(timezone.utc)
