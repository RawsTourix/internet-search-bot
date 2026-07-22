"""Compatibility exports for manager-tool schema normalization."""

from ..mcp.schema import SchemaNormalizationError, inline_local_schema_refs

PlanningSchemaError = SchemaNormalizationError

__all__ = [
    "PlanningSchemaError",
    "SchemaNormalizationError",
    "inline_local_schema_refs",
]
