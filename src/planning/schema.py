"""JSON Schema normalization for broad OpenAI-compatible tool support."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


class PlanningSchemaError(ValueError):
    """Raised when a local schema reference cannot be resolved safely."""


def inline_local_schema_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Expand local ``#/$defs/...`` references and remove the definitions map.

    Planning tool models are finite and non-recursive. Inlining keeps function
    schemas compatible with providers that accept nested JSON Schema but reject
    ``$defs``/``$ref`` in tool parameters.
    """
    source = deepcopy(schema)
    definitions = source.get("$defs")
    if definitions is None:
        return source
    if not isinstance(definitions, dict):
        raise PlanningSchemaError("$defs must be an object")

    def expand(value: Any, stack: tuple[str, ...] = ()) -> Any:
        if isinstance(value, list):
            return [expand(item, stack) for item in value]
        if not isinstance(value, dict):
            return value

        reference = value.get("$ref")
        if reference is not None:
            if not isinstance(reference, str) or not reference.startswith("#/$defs/"):
                raise PlanningSchemaError("Only local $defs references are supported")
            name = reference.removeprefix("#/$defs/")
            if name not in definitions:
                raise PlanningSchemaError(f"Unknown schema definition: {name}")
            if name in stack:
                raise PlanningSchemaError("Recursive planning tool schema is unsupported")
            resolved = expand(definitions[name], (*stack, name))
            if not isinstance(resolved, dict):
                raise PlanningSchemaError("Referenced schema must be an object")
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
        raise PlanningSchemaError("Root schema must be an object")
    return normalized
