"""Deterministic validation for native text artifact formats."""

from __future__ import annotations

import csv
import io
import json

from pydantic import BaseModel, ConfigDict, Field

from .errors import ArtifactValidationError


class ArtifactValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    valid: bool = True
    format_id: str
    warnings: list[str] = Field(default_factory=list)
    details: dict[str, int | str | bool] = Field(default_factory=dict)


def validate_native_text(
    *,
    format_id: str,
    text: str,
    max_csv_rows: int = 100_000,
    max_csv_columns: int = 10_000,
) -> ArtifactValidationReport:
    """Validate syntax we can check safely without executing file contents."""

    normalized = format_id.strip().lower()
    if normalized == "json":
        try:
            json.loads(text)
        except (json.JSONDecodeError, RecursionError) as error:
            raise ArtifactValidationError(
                "invalid_json_artifact",
                "JSON artifact content is invalid.",
                retryable=True,
                details={
                    "line": getattr(error, "lineno", 0),
                    "column": getattr(error, "colno", 0),
                },
            ) from error
        return ArtifactValidationReport(format_id=normalized)

    if normalized == "csv":
        rows = 0
        max_columns_seen = 0
        try:
            reader = csv.reader(io.StringIO(text, newline=""))
            for row in reader:
                rows += 1
                if rows > max_csv_rows:
                    raise ArtifactValidationError(
                        "csv_row_limit_exceeded",
                        "CSV artifact exceeds the validation row limit.",
                        retryable=True,
                        details={"max_rows": max_csv_rows},
                    )
                max_columns_seen = max(max_columns_seen, len(row))
                if max_columns_seen > max_csv_columns:
                    raise ArtifactValidationError(
                        "csv_column_limit_exceeded",
                        "CSV artifact exceeds the validation column limit.",
                        retryable=True,
                        details={"max_columns": max_csv_columns},
                    )
        except csv.Error as error:
            raise ArtifactValidationError(
                "invalid_csv_artifact",
                "CSV artifact content is invalid.",
                retryable=True,
            ) from error
        return ArtifactValidationReport(
            format_id=normalized,
            details={"rows": rows, "max_columns": max_columns_seen},
        )

    if normalized == "yaml":
        return ArtifactValidationReport(
            format_id=normalized,
            warnings=["yaml_syntax_not_parsed"],
        )

    return ArtifactValidationReport(format_id=normalized)
