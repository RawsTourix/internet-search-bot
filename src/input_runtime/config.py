"""Input-runtime configuration loader and safe diagnostics."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from .errors import InputRuntimeConfigValidationError


class InputRuntimeConfigType(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    max_queued_batches_per_session: int = 64
    max_queued_bytes_per_session: int = 268_435_456
    max_batches_per_checkpoint: int = 8
    max_batch_bytes_per_checkpoint: int = 67_108_864
    claim_lease_seconds: int = 300
    max_intermediate_messages_per_cycle: int = 16
    min_intermediate_message_interval_seconds: float = 15.0
    max_intermediate_message_chars: int = 3500

    @field_validator(
        "max_queued_batches_per_session", "max_queued_bytes_per_session",
        "max_batches_per_checkpoint", "max_batch_bytes_per_checkpoint",
        "claim_lease_seconds", "max_intermediate_messages_per_cycle",
        "max_intermediate_message_chars",
    )
    @classmethod
    def validate_positive_integer(cls, value: int) -> int:
        if isinstance(value, bool) or value <= 0:
            raise ValueError("input-runtime limits must be positive integers")
        return value

    @field_validator("min_intermediate_message_interval_seconds")
    @classmethod
    def validate_interval(cls, value: float) -> float:
        if not math.isfinite(value) or value < 0:
            raise ValueError("intermediate message interval must be finite and non-negative")
        return value

    @model_validator(mode="after")
    def validate_relations(self) -> "InputRuntimeConfigType":
        if self.max_batches_per_checkpoint > self.max_queued_batches_per_session:
            raise ValueError("max_batches_per_checkpoint cannot exceed queue batch limit")
        if self.max_batch_bytes_per_checkpoint > self.max_queued_bytes_per_session:
            raise ValueError("max_batch_bytes_per_checkpoint cannot exceed queue byte limit")
        return self


def parse_input_runtime_config(raw: Mapping[str, Any] | None) -> InputRuntimeConfigType:
    try:
        if raw is None:
            return InputRuntimeConfigType()
        if not isinstance(raw, Mapping):
            raise TypeError("input_runtime section must be an object")
        return InputRuntimeConfigType.model_validate(dict(raw))
    except Exception as error:
        raise InputRuntimeConfigValidationError(
            f"Invalid input_runtime configuration: {error}"
        ) from error


def load_input_runtime_config(config_path: str | Path) -> InputRuntimeConfigType:
    path = Path(config_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("configuration root must be a JSON object")
        section = payload.get("input_runtime")
        if section is None and "input_runtime" in payload:
            raise TypeError("input_runtime section must be an object")
        return parse_input_runtime_config(section)
    except InputRuntimeConfigValidationError:
        raise
    except Exception as error:
        raise InputRuntimeConfigValidationError(
            f"Cannot load input_runtime configuration from {path}: {error}"
        ) from error


def safe_input_runtime_config_summary(config: InputRuntimeConfigType) -> dict[str, Any]:
    return config.model_dump(mode="json")
