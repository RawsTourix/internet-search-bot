"""Bind trusted local MCP processors to artifact workspace transport."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .config import ArtifactConfigType
from .errors import ArtifactConfigValidationError


def apply_local_workspace_server_policy(
    server_configs: Iterable[Any],
    artifact_config: ArtifactConfigType,
) -> None:
    """Apply validated runtime-only transport markers to MCP server configs.

    The current MCP ``ServerConfigType`` predates artifacts and therefore does
    not persist this policy field yet.  This composition-boundary adapter keeps
    the permission explicit and validated while allowing a future native
    ``artifact_transport`` field to replace the runtime marker without changing
    workspace or manager-tool contracts.
    """

    configs = list(server_configs)
    by_name: dict[str, Any] = {}
    duplicate_names: list[str] = []
    for item in configs:
        name = str(getattr(item, "name", None) or "unnamed").strip()
        if not name:
            name = "unnamed"
        if name in by_name:
            duplicate_names.append(name)
        else:
            by_name[name] = item

    if duplicate_names:
        values = sorted(set(duplicate_names))
        raise ArtifactConfigValidationError(
            "Duplicate MCP server names cannot be used by artifact policy: "
            + ", ".join(values)
        )

    requested = list(artifact_config.local_workspace_server_names)
    unknown = sorted(name for name in requested if name not in by_name)
    if unknown:
        raise ArtifactConfigValidationError(
            "Unknown local workspace MCP server names: " + ", ".join(unknown)
        )

    invalid_transport: list[str] = []
    for name in requested:
        item = by_name[name]
        connect_type = getattr(item, "connect_type", None)
        connect_value = getattr(connect_type, "value", connect_type)
        if connect_value != "executable":
            invalid_transport.append(name)
    if invalid_transport:
        raise ArtifactConfigValidationError(
            "Local artifact workspaces require executable MCP servers: "
            + ", ".join(sorted(invalid_transport))
        )

    allowed = set(requested)
    # Mutate only after every validation has succeeded, so a rejected policy
    # cannot leave a partially authorized server set.
    for name, item in by_name.items():
        object.__setattr__(
            item,
            "artifact_transport",
            "local_workspace" if name in allowed else "none",
        )
