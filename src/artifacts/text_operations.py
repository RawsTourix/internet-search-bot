"""Deterministic native-text operations used by ArtifactService."""

from __future__ import annotations

from collections.abc import Sequence

from .config import ArtifactConfigType
from .errors import ArtifactLimitError, ArtifactValidationError
from .models import ExactTextPatchOperation


def enforce_text_size(
    text: str,
    *,
    encoding: str,
    max_bytes: int,
    operation: str,
) -> bytes:
    try:
        payload = text.encode(encoding)
    except (LookupError, UnicodeEncodeError) as error:
        raise ArtifactValidationError(
            "artifact_text_encoding_error",
            f"Artifact text cannot be encoded as {encoding}.",
            retryable=True,
            details={"operation": operation},
        ) from error
    if len(payload) > max_bytes:
        raise ArtifactLimitError(
            f"Artifact text exceeds the {operation} byte limit"
        )
    return payload


def apply_exact_text_patch(
    text: str,
    operations: Sequence[ExactTextPatchOperation],
    *,
    config: ArtifactConfigType,
    encoding: str,
) -> str:
    """Apply all exact replacements atomically or raise before persistence."""

    if not operations:
        raise ArtifactValidationError(
            "empty_artifact_patch",
            "Artifact patch must contain at least one operation.",
            retryable=True,
        )
    if len(operations) > config.max_patch_operations:
        raise ArtifactLimitError("Artifact patch operation limit exceeded")

    current = text
    for index, operation in enumerate(operations):
        if len(operation.old_text) > config.max_patch_old_text_chars:
            raise ArtifactLimitError("Artifact patch old_text limit exceeded")
        if len(operation.new_text) > config.max_patch_new_text_chars:
            raise ArtifactLimitError("Artifact patch new_text limit exceeded")

        actual = current.count(operation.old_text)
        if actual != operation.expected_occurrences:
            raise ArtifactValidationError(
                "artifact_patch_conflict",
                "Artifact patch did not match the expected current text.",
                retryable=True,
                details={
                    "operation_index": index,
                    "expected_occurrences": operation.expected_occurrences,
                    "actual_occurrences": actual,
                },
            )
        current = current.replace(
            operation.old_text,
            operation.new_text,
            operation.expected_occurrences,
        )
        enforce_text_size(
            current,
            encoding=encoding,
            max_bytes=config.max_patchable_text_bytes,
            operation="patch",
        )
    return current
