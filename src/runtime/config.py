"""Configuration for agent runtime lifecycle behavior."""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, field_validator


class RuntimeConfigType(BaseModel):
    """Validated runtime settings that are independent of LLM and memory."""

    model_config = ConfigDict(extra="forbid")

    mcp_startup_timeout: float = 30.0
    mcp_transport_call_timeout: float = 15.0
    mcp_reconnect_timeout: float = 10.0
    mcp_runtime_close_timeout: float = 10.0
    mcp_call_retries_after_recovery: int = 1

    @field_validator(
        "mcp_startup_timeout",
        "mcp_transport_call_timeout",
        "mcp_reconnect_timeout",
        "mcp_runtime_close_timeout",
    )
    @classmethod
    def validate_positive_timeout(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("runtime timeouts must be positive and finite")
        return value

    @field_validator("mcp_call_retries_after_recovery")
    @classmethod
    def validate_recovery_retries(cls, value: int) -> int:
        if not 0 <= value <= 5:
            raise ValueError(
                "mcp_call_retries_after_recovery must be between 0 and 5"
            )
        return value
