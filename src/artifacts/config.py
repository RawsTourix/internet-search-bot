"""Configuration for the v0.4 artifact foundation."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .errors import ArtifactConfigValidationError


class ArtifactConfigType(BaseModel):
    """Runtime budgets and policies for artifact handling."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True

    max_artifacts_per_cycle: int = Field(default=32, ge=1)
    max_versions_per_lineage: int = Field(default=64, ge=1)
    max_artifact_size_bytes: int = Field(default=64 * 1024 * 1024, ge=1)

    max_inline_text_chars: int = Field(default=20_000, ge=1)
    max_read_chars: int = Field(default=100_000, ge=1)
    max_search_matches: int = Field(default=20, ge=1)

    max_patch_operations: int = Field(default=32, ge=1)
    max_patchable_text_bytes: int = Field(default=8 * 1024 * 1024, ge=1)
    max_patch_old_text_chars: int = Field(default=20_000, ge=1)
    max_patch_new_text_chars: int = Field(default=50_000, ge=0)

    max_runtime_artifact_summaries: int = Field(default=12, ge=1)
    # Internal process-safety limits. They are not exposed in manager schemas.
    max_concurrent_artifact_reads: int = Field(default=4, ge=1)
    max_composite_result_bytes: int = Field(
        default=8 * 1024 * 1024,
        ge=1,
    )

    allow_opaque_binary: bool = True
    auto_select_deliverables: bool = False
    local_workspace_server_names: list[str] = Field(default_factory=list)

    max_container_entries_inspected: int = Field(default=2_048, ge=1)
    max_workspace_bytes: int = Field(default=256 * 1024 * 1024, ge=1)
    workspace_ttl_seconds: int = Field(default=3_600, ge=1)
    delivery_claim_timeout_seconds: int = Field(default=900, ge=1)

    @field_validator("local_workspace_server_names")
    @classmethod
    def normalize_local_workspace_servers(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            normalized = value.strip()
            if not normalized:
                raise ValueError("local workspace server name must not be empty")
            if normalized not in seen:
                result.append(normalized)
                seen.add(normalized)
        return result

    @model_validator(mode="after")
    def validate_cross_field_limits(self) -> "ArtifactConfigType":
        if self.max_inline_text_chars > self.max_read_chars:
            raise ValueError(
                "max_inline_text_chars must not exceed max_read_chars"
            )
        if self.max_patchable_text_bytes > self.max_artifact_size_bytes:
            raise ValueError(
                "max_patchable_text_bytes must not exceed max_artifact_size_bytes"
            )
        if self.max_workspace_bytes < self.max_artifact_size_bytes:
            raise ValueError(
                "max_workspace_bytes must be at least max_artifact_size_bytes"
            )
        if self.max_concurrent_artifact_reads > self.max_artifacts_per_cycle:
            raise ValueError(
                "max_concurrent_artifact_reads must not exceed "
                "max_artifacts_per_cycle"
            )
        return self


def load_artifact_config(config_path: str) -> ArtifactConfigType:
    """Load the optional root-level ``artifacts`` section with defaults."""

    try:
        payload = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ArtifactConfigValidationError(
            "Failed to read artifact configuration"
        ) from error

    if not isinstance(payload, dict):
        raise ArtifactConfigValidationError("Configuration root must be an object")

    artifact_data = payload.get("artifacts", {})
    if artifact_data is None:
        artifact_data = {}
    if not isinstance(artifact_data, dict):
        raise ArtifactConfigValidationError(
            "Artifact configuration must be an object"
        )

    try:
        return ArtifactConfigType.model_validate(artifact_data)
    except ValidationError as error:
        raise ArtifactConfigValidationError(
            "Invalid artifact configuration"
        ) from error
