"""Configuration for durable client ingress and atomic input batches."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class IngressConfigValidationError(RuntimeError):
    """Raised when the root-level ingress configuration is invalid."""


class IngressConfigType(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    max_events_per_batch: int = Field(default=64, ge=1)
    max_attachments_per_batch: int = Field(default=32, ge=0)
    max_batch_total_bytes: int = Field(default=256 * 1024 * 1024, ge=1)
    max_text_parts_per_batch: int = Field(default=64, ge=0)
    max_text_part_chars: int = Field(default=100_000, ge=1)

    @model_validator(mode="after")
    def validate_part_limits(self) -> "IngressConfigType":
        if self.max_attachments_per_batch == 0 and self.max_text_parts_per_batch == 0:
            raise ValueError("ingress must allow text parts or attachments")
        return self


def load_ingress_config(config_path: str) -> IngressConfigType:
    """Load optional root-level ``ingress`` settings with safe defaults."""
    try:
        payload = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise IngressConfigValidationError(
            "Failed to read ingress configuration"
        ) from error
    if not isinstance(payload, dict):
        raise IngressConfigValidationError("Configuration root must be an object")
    ingress_data = payload.get("ingress", {})
    if ingress_data is None:
        ingress_data = {}
    if not isinstance(ingress_data, dict):
        raise IngressConfigValidationError(
            "Ingress configuration must be an object"
        )
    try:
        return IngressConfigType.model_validate(ingress_data)
    except ValidationError as error:
        raise IngressConfigValidationError(
            "Invalid ingress configuration"
        ) from error
