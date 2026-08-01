"""Audit that public configuration examples cover every supported parameter.

The project deliberately keeps runtime defaults, but every user-configurable
parameter must still be visible in one canonical example:

* environment variables read by production code -> ``.env.example``;
* every field of a root MCP/agent config model -> ``mcp.config.example``.

This module is importable from tests and executable as a small maintenance tool.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel

from src.artifacts import ArtifactConfigType
from src.ingress import IngressConfigType
from src.interaction.config import (
    ClientCapabilitiesConfig,
    InputPresentationConfig,
    LocalizationConfigType,
    OutputRuntimeConfig,
    TelegramOutputConfig,
)
from src.memory import MemoryConfigType
from src.mcp.mcp_client import LLMConfigType, ServerConfigType
from src.planning import PlanningConfigType
from src.runtime import RuntimeConfigType
from src.storage import StorageConfigType


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
ENV_EXAMPLE_PATH = REPOSITORY_ROOT / ".env.example"
MCP_CONFIG_EXAMPLE_PATH = REPOSITORY_ROOT / "src" / "api" / "mcp.config.example"

_ENV_ASSIGNMENT = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")

# One canonical mapping from persisted root section to its authoritative model.
# Adding a new field to any model is caught automatically. Adding a new root
# section must extend this registry and the example in the same patch.
CONFIG_SECTION_MODELS: dict[str, type[BaseModel]] = {
    "llm": LLMConfigType,
    "runtime": RuntimeConfigType,
    "storage": StorageConfigType,
    "memory": MemoryConfigType,
    "artifacts": ArtifactConfigType,
    "ingress": IngressConfigType,
    "client_capabilities": ClientCapabilitiesConfig,
    "localization": LocalizationConfigType,
    "input_presentation": InputPresentationConfig,
    "output_runtime": OutputRuntimeConfig,
    "telegram_output": TelegramOutputConfig,
    "planning": PlanningConfigType,
}


class _EnvironmentReadVisitor(ast.NodeVisitor):
    """Collect literal environment keys read through supported Python idioms."""

    def __init__(self) -> None:
        self.keys: set[str] = set()

    @staticmethod
    def _literal_key(node: ast.AST | None) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value.strip()
            return value or None
        return None

    @staticmethod
    def _is_os_environ(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "environ"
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
        )

    def visit_Call(self, node: ast.Call) -> Any:
        function = node.func
        key: str | None = None
        if (
            isinstance(function, ast.Attribute)
            and function.attr == "getenv"
            and isinstance(function.value, ast.Name)
            and function.value.id == "os"
        ):
            key = self._literal_key(node.args[0] if node.args else None)
        elif (
            isinstance(function, ast.Attribute)
            and function.attr == "get"
            and self._is_os_environ(function.value)
        ):
            key = self._literal_key(node.args[0] if node.args else None)
        elif isinstance(function, ast.Name) and function.id == "getenv":
            # Supports ``from os import getenv`` without executing imports. A
            # literal key keeps false positives acceptably narrow.
            key = self._literal_key(node.args[0] if node.args else None)
        if key is not None:
            self.keys.add(key)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> Any:
        # Only reads count. Internal writes such as
        # ``os.environ["http_proxy"] = ...`` are implementation details rather
        # than public configuration inputs.
        if isinstance(node.ctx, ast.Load) and self._is_os_environ(node.value):
            key = self._literal_key(node.slice)
            if key is not None:
                self.keys.add(key)
        self.generic_visit(node)


def iter_production_python_files(root: Path = REPOSITORY_ROOT) -> Iterable[Path]:
    """Yield source, maintenance scripts and root Python entrypoints.

    Tests are deliberately excluded because their temporary fixture variables
    are not public runtime configuration.
    """

    paths: set[Path] = set(root.glob("*.py"))
    for directory_name in ("src", "scripts"):
        directory = root / directory_name
        if directory.exists():
            paths.update(directory.rglob("*.py"))
    return sorted(path for path in paths if "__pycache__" not in path.parts)


def discover_environment_reads(root: Path = REPOSITORY_ROOT) -> set[str]:
    keys: set[str] = set()
    for path in iter_production_python_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _EnvironmentReadVisitor()
        visitor.visit(tree)
        keys.update(visitor.keys)
    return keys


def parse_env_example(path: Path = ENV_EXAMPLE_PATH) -> set[str]:
    keys: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        match = _ENV_ASSIGNMENT.match(line)
        if match:
            keys.add(match.group(1))
    return keys


def load_mcp_config_example(path: Path = MCP_CONFIG_EXAMPLE_PATH) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AssertionError("mcp.config.example root must be a JSON object")
    return payload


def missing_environment_example_keys() -> list[str]:
    return sorted(discover_environment_reads() - parse_env_example())


def missing_config_example_fields() -> dict[str, list[str]]:
    payload = load_mcp_config_example()
    missing: dict[str, list[str]] = {}

    servers = payload.get("servers")
    if not isinstance(servers, list) or not servers:
        missing["servers"] = sorted(ServerConfigType.model_fields)
    else:
        represented_server_fields: set[str] = set()
        for item in servers:
            if isinstance(item, dict):
                represented_server_fields.update(item)
        absent = sorted(set(ServerConfigType.model_fields) - represented_server_fields)
        if absent:
            missing["servers[*]"] = absent

    for section, model in CONFIG_SECTION_MODELS.items():
        value = payload.get(section)
        if not isinstance(value, dict):
            missing[section] = sorted(model.model_fields)
            continue
        absent = sorted(set(model.model_fields) - set(value))
        if absent:
            missing[section] = absent
    return missing


def validate_config_example_values() -> None:
    """Validate example values against every authoritative Pydantic model."""

    payload = load_mcp_config_example()
    servers = payload.get("servers")
    if not isinstance(servers, list) or not servers:
        raise AssertionError("mcp.config.example must contain at least one server")
    for index, item in enumerate(servers):
        try:
            ServerConfigType.model_validate(item)
        except Exception as error:  # pragma: no cover - message asserted by CI
            raise AssertionError(f"servers[{index}] is invalid: {error}") from error

    for section, model in CONFIG_SECTION_MODELS.items():
        try:
            model.model_validate(payload.get(section))
        except Exception as error:  # pragma: no cover - message asserted by CI
            raise AssertionError(f"{section} is invalid: {error}") from error


def main() -> int:
    env_missing = missing_environment_example_keys()
    config_missing = missing_config_example_fields()
    errors: list[str] = []
    if env_missing:
        errors.append(".env.example is missing: " + ", ".join(env_missing))
    if config_missing:
        details = "; ".join(
            f"{section}: {', '.join(fields)}"
            for section, fields in sorted(config_missing.items())
        )
        errors.append("mcp.config.example is missing fields: " + details)
    try:
        validate_config_example_values()
    except AssertionError as error:
        errors.append(str(error))

    if errors:
        print("Configuration example audit failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Configuration examples cover all discovered parameters.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
