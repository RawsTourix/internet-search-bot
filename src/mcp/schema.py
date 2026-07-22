"""JSON Schema normalization shared by manager-tool domains."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


class SchemaNormalizationError(ValueError):
    """Raised when a local schema reference cannot be resolved safely."""


def inline_local_schema_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Expand finite local ``#/$defs/...`` references for provider support."""
    source = deepcopy(schema)
    definitions = source.get("$defs")
    if definitions is None:
        return source
    if not isinstance(definitions, dict):
        raise SchemaNormalizationError("$defs must be an object")

    def expand(value: Any, stack: tuple[str, ...] = ()) -> Any:
        if isinstance(value, list):
            return [expand(item, stack) for item in value]
        if not isinstance(value, dict):
            return value

        reference = value.get("$ref")
        if reference is not None:
            if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
                raise SchemaNormalizationError(
                    "Only local $defs references are supported"
                )
            name = reference.removeprefix("#/$defs/")
            if name not in definitions:
                raise SchemaNormalizationError(
                    f"Unknown schema definition: {name}"
                )
            if name in stack:
                raise SchemaNormalizationError(
                    "Recursive manager-tool schema is unsupported"
                )
            resolved = expand(definitions[name], (*stack, name))
            if not isinstance(resolved, dict):
                raise SchemaNormalizationError(
                    "Referenced schema must be an object"
                )
            siblings = {
                key: expand(item, stack)
                for key, item in value.items()
                if key != "$ref"
            }
            return {**resolved, **siblings}

        return {
            key: expand(item, stack)
            for key, item in value.items()
            if key != "$defs"
        }

    normalized = expand(source)
    if not isinstance(normalized, dict):
        raise SchemaNormalizationError("Root schema must be an object")
    return normalized
