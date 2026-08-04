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
from dataclasses import dataclass
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
# Adding a new field to any model is caught automatically. Root sections are
# also discovered from load_*config functions, so a newly introduced section
# must be registered here and added to the example in the same patch.
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
CONFIG_ROOT_ALIASES = frozenset({"server"})
CONFIG_ROOT_NON_MODEL_SECTIONS = frozenset({"servers"})


@dataclass(frozen=True, slots=True)
class EnvironmentReadAudit:
    keys: frozenset[str]
    unresolved: tuple[str, ...]


class _EnvironmentReadVisitor(ast.NodeVisitor):
    """Collect literal and statically resolvable environment reads."""

    def __init__(self, *, filename: str) -> None:
        self.filename = filename
        self.keys: set[str] = set()
        self.unresolved: set[str] = set()
        self.bindings: dict[str, set[str]] = {}

    def _resolve_strings(self, node: ast.AST | None) -> set[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value.strip()
            return {value} if value else set()
        if isinstance(node, ast.Name):
            return set(self.bindings.get(node.id, set()))
        return set()

    @staticmethod
    def _is_os_environ(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and node.attr == "environ"
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
        )

    def _record_read(self, node: ast.AST, argument: ast.AST | None) -> None:
        values = self._resolve_strings(argument)
        if values:
            self.keys.update(values)
            return
        line = getattr(node, "lineno", "?")
        self.unresolved.add(f"{self.filename}:{line}")

    def visit_Assign(self, node: ast.Assign) -> Any:
        values = self._resolve_strings(node.value)
        if values:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.bindings[target.id] = set(values)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> Any:
        values = self._resolve_strings(node.value)
        if values and isinstance(node.target, ast.Name):
            self.bindings[node.target.id] = set(values)
        self.generic_visit(node)

    @staticmethod
    def _for_bindings(target: ast.AST, iterator: ast.AST) -> dict[str, set[str]]:
        if not isinstance(iterator, (ast.Tuple, ast.List)):
            return {}
        items = list(iterator.elts)
        if isinstance(target, ast.Name):
            values = {
                str(item.value).strip()
                for item in items
                if isinstance(item, ast.Constant)
                and isinstance(item.value, str)
                and str(item.value).strip()
            }
            return {target.id: values} if values else {}
        if not isinstance(target, (ast.Tuple, ast.List)):
            return {}

        bindings: dict[str, set[str]] = {}
        target_items = list(target.elts)
        for index, target_item in enumerate(target_items):
            if not isinstance(target_item, ast.Name):
                continue
            values: set[str] = set()
            for item in items:
                if not isinstance(item, (ast.Tuple, ast.List)):
                    continue
                if index >= len(item.elts):
                    continue
                value_node = item.elts[index]
                if isinstance(value_node, ast.Constant) and isinstance(
                    value_node.value, str
                ):
                    value = value_node.value.strip()
                    if value:
                        values.add(value)
            if values:
                bindings[target_item.id] = values
        return bindings

    def visit_For(self, node: ast.For) -> Any:
        loop_bindings = self._for_bindings(node.target, node.iter)
        previous = {
            name: self.bindings.get(name)
            for name in loop_bindings
        }
        self.bindings.update(loop_bindings)
        self.visit(node.iter)
        for statement in node.body:
            self.visit(statement)
        for name, value in previous.items():
            if value is None:
                self.bindings.pop(name, None)
            else:
                self.bindings[name] = value
        for statement in node.orelse:
            self.visit(statement)

    def visit_Call(self, node: ast.Call) -> Any:
        function = node.func
        is_environment_read = False
        if (
            isinstance(function, ast.Attribute)
            and function.attr == "getenv"
            and isinstance(function.value, ast.Name)
            and function.value.id == "os"
        ):
            is_environment_read = True
        elif (
            isinstance(function, ast.Attribute)
            and function.attr == "get"
            and self._is_os_environ(function.value)
        ):
            is_environment_read = True
        elif isinstance(function, ast.Name) and function.id == "getenv":
            is_environment_read = True

        if is_environment_read:
            self._record_read(node, node.args[0] if node.args else None)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> Any:
        # Only reads count. Internal writes such as
        # ``os.environ["http_proxy"] = ...`` are implementation details rather
        # than public configuration inputs.
        if isinstance(node.ctx, ast.Load) and self._is_os_environ(node.value):
            self._record_read(node, node.slice)
        self.generic_visit(node)


class _RootConfigSectionVisitor(ast.NodeVisitor):
    """Find root JSON sections read by canonical load_*config functions."""

    def __init__(self) -> None:
        self.sections: set[str] = set()
        self._inside_loader = 0

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        is_loader = node.name.startswith("load_") and "config" in node.name
        if is_loader:
            self._inside_loader += 1
        for statement in node.body:
            self.visit(statement)
        if is_loader:
            self._inside_loader -= 1

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        is_loader = node.name.startswith("load_") and "config" in node.name
        if is_loader:
            self._inside_loader += 1
        for statement in node.body:
            self.visit(statement)
        if is_loader:
            self._inside_loader -= 1

    def visit_Call(self, node: ast.Call) -> Any:
        function = node.func
        if (
            self._inside_loader
            and isinstance(function, ast.Attribute)
            and function.attr == "get"
            and isinstance(function.value, ast.Name)
            and function.value.id in {"config", "payload"}
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            value = node.args[0].value.strip()
            if value:
                self.sections.add(value)
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


def audit_environment_reads(root: Path = REPOSITORY_ROOT) -> EnvironmentReadAudit:
    keys: set[str] = set()
    unresolved: set[str] = set()
    for path in iter_production_python_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _EnvironmentReadVisitor(
            filename=str(path.relative_to(root)).replace("\\", "/")
        )
        visitor.visit(tree)
        keys.update(visitor.keys)
        unresolved.update(visitor.unresolved)
    return EnvironmentReadAudit(
        keys=frozenset(keys),
        unresolved=tuple(sorted(unresolved)),
    )


def discover_environment_reads(root: Path = REPOSITORY_ROOT) -> set[str]:
    return set(audit_environment_reads(root).keys)


def discover_config_root_sections(root: Path = REPOSITORY_ROOT) -> set[str]:
    sections: set[str] = set()
    for path in iter_production_python_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        visitor = _RootConfigSectionVisitor()
        visitor.visit(tree)
        sections.update(visitor.sections)
    return sections


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


def unresolved_environment_reads() -> list[str]:
    return list(audit_environment_reads().unresolved)


def missing_environment_example_keys() -> list[str]:
    return sorted(discover_environment_reads() - parse_env_example())


def missing_config_example_sections() -> list[str]:
    payload = load_mcp_config_example()
    required = discover_config_root_sections() - CONFIG_ROOT_ALIASES
    return sorted(required - set(payload))


def unregistered_config_sections() -> list[str]:
    discovered = discover_config_root_sections() - CONFIG_ROOT_ALIASES
    registered = set(CONFIG_SECTION_MODELS) | set(CONFIG_ROOT_NON_MODEL_SECTIONS)
    return sorted(discovered - registered)


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
    env_unresolved = unresolved_environment_reads()
    config_sections_missing = missing_config_example_sections()
    config_sections_unregistered = unregistered_config_sections()
    config_missing = missing_config_example_fields()
    errors: list[str] = []
    if env_missing:
        errors.append(".env.example is missing: " + ", ".join(env_missing))
    if env_unresolved:
        errors.append(
            "environment reads cannot be resolved statically: "
            + ", ".join(env_unresolved)
        )
    if config_sections_missing:
        errors.append(
            "mcp.config.example is missing root sections: "
            + ", ".join(config_sections_missing)
        )
    if config_sections_unregistered:
        errors.append(
            "root config sections are not registered for field audit: "
            + ", ".join(config_sections_unregistered)
        )
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
