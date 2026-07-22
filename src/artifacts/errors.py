"""Managed errors for the artifact domain."""

from __future__ import annotations

from typing import Any


class ArtifactError(RuntimeError):
    """Base error for artifact domain and persistence operations."""


class ArtifactConfigValidationError(ArtifactError):
    """Raised when artifact configuration is invalid."""


class ArtifactNotFoundError(ArtifactError):
    """Raised when an artifact lineage or exact version does not exist."""


class ArtifactAccessError(ArtifactError):
    """Raised when an artifact is outside the current runtime authority."""


class ArtifactStorageError(ArtifactError):
    """Raised when artifact metadata cannot be persisted or loaded."""


class ArtifactIntegrityError(ArtifactError):
    """Raised when artifact metadata and referenced content disagree."""


class ArtifactCapabilityError(ArtifactError):
    """Raised when an operation is unsupported for an artifact format."""


class ArtifactLimitError(ArtifactError):
    """Raised when a configured artifact budget would be exceeded."""


class ArtifactCandidateError(ArtifactError):
    """Raised when a candidate cannot be promoted safely."""


class ArtifactWorkspaceError(ArtifactError):
    """Raised when an isolated MCP artifact workspace is invalid or unsafe."""


class ArtifactDeliveryError(ArtifactError):
    """Raised when a delivery state transition is invalid."""


class ArtifactDeliveryNotFoundError(ArtifactDeliveryError):
    """Raised when a delivery record does not exist."""


class ArtifactValidationError(ArtifactError):
    """Structured validation failure suitable for manager-tool output."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = True,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.safe_message = message
        self.retryable = retryable
        self.details = dict(details or {})


class ArtifactVersionConflictError(ArtifactError):
    """Optimistic-concurrency conflict for one artifact lineage."""

    def __init__(
        self,
        artifact_lineage_id: str,
        *,
        expected_current_artifact_id: str,
        current_artifact_id: str,
        current_version: int,
        current_ref: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            f"Artifact version conflict for {artifact_lineage_id}: "
            f"expected {expected_current_artifact_id}, "
            f"current {current_artifact_id}"
        )
        self.artifact_lineage_id = artifact_lineage_id
        self.expected_current_artifact_id = expected_current_artifact_id
        self.current_artifact_id = current_artifact_id
        self.current_version = current_version
        self.current_ref = dict(current_ref or {})
        self.retryable = True
