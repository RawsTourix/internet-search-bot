"""Audit that public configuration examples cover every supported parameter."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel

from src.artifacts import ArtifactConfigType
from src.ingress import IngressConfigType
from src.input_runtime import InputRuntimeConfigType
from src.interaction.config import (
    ClientCapabilitiesConfig, InputPresentationConfig, LocalizationConfigType,
    OutputRuntimeConfig, TelegramOutputConfig,
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

CONFIG_SECTION_MODELS: dict[str, type[BaseModel]] = {
    "llm": LLMConfigType,
    "runtime": RuntimeConfigType,
    "storage": StorageConfigType,
    "memory": MemoryConfigType,
    "artifacts": ArtifactConfigType,
    "ingress": IngressConfigType,
    "input_runtime": InputRuntimeConfigType,
    "client_capabilities": ClientCapabilitiesConfig,
    "localization": LocalizationConfigType,
    "input_presentation": InputPresentationConfig,
    "output_runtime": OutputRuntimeConfig,
    "telegram_output": TelegramOutputConfig,
    "planning": PlanningConfigType,
}
CONFIG_ROOT_ALIASES = frozenset({"server"})
CONFIG_ROOT_NON_MODEL_SECTIONS = frozenset({"servers"})


def iter_production_python_files(root: Path = REPOSITORY_ROOT) -> Iterable[Path]:
    paths = set(root.glob("*.py"))
    for name in ("src", "scripts"):
        directory = root / name
        if directory.exists():
            paths.update(directory.rglob("*.py"))
    return sorted(path for path in paths if "__pycache__" not in path.parts)


def _literal_strings(node: ast.AST | None, bindings: dict[str, set[str]]) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value.strip()} if node.value.strip() else set()
    if isinstance(node, ast.Name):
        return bindings.get(node.id, set())
    return set()


def _record_loop_bindings(node: ast.For, bindings: dict[str, set[str]]) -> None:
    if not isinstance(node.iter, (ast.Tuple, ast.List)):
        return
    items = list(node.iter.elts)
    if isinstance(node.target, ast.Name):
        values = {
            item.value.strip() for item in items
            if isinstance(item, ast.Constant) and isinstance(item.value, str) and item.value.strip()
        }
        if values:
            bindings[node.target.id] = values
        return
    if not isinstance(node.target, (ast.Tuple, ast.List)):
        return
    for index, target in enumerate(node.target.elts):
        if not isinstance(target, ast.Name):
            continue
        values: set[str] = set()
        for item in items:
            if not isinstance(item, (ast.Tuple, ast.List)) or index >= len(item.elts):
                continue
            value = item.elts[index]
            if isinstance(value, ast.Constant) and isinstance(value.value, str) and value.value.strip():
                values.add(value.value.strip())
        if values:
            bindings[target.id] = values


def _environment_audit(root: Path = REPOSITORY_ROOT) -> tuple[set[str], list[str]]:
    keys: set[str] = set()
    unresolved: set[str] = set()
    for path in iter_production_python_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        bindings: dict[str, set[str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = _literal_strings(node.value, bindings)
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if value and isinstance(target, ast.Name):
                        bindings[target.id] = value
            elif isinstance(node, ast.For):
                _record_loop_bindings(node, bindings)
        for node in ast.walk(tree):
            argument = None
            is_read = False
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "getenv" and isinstance(func.value, ast.Name) and func.value.id == "os":
                    is_read = True
                elif isinstance(func, ast.Attribute) and func.attr == "get" and isinstance(func.value, ast.Attribute) and func.value.attr == "environ":
                    is_read = True
                elif isinstance(func, ast.Name) and func.id == "getenv":
                    is_read = True
                if is_read:
                    argument = node.args[0] if node.args else None
            elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load) and isinstance(node.value, ast.Attribute) and node.value.attr == "environ":
                is_read = True
                argument = node.slice
            if is_read:
                values = _literal_strings(argument, bindings)
                if values:
                    keys.update(values)
                else:
                    unresolved.add(f"{path.relative_to(root).as_posix()}:{getattr(node, 'lineno', '?')}")
    return keys, sorted(unresolved)


def discover_environment_reads(root: Path = REPOSITORY_ROOT) -> set[str]:
    return _environment_audit(root)[0]


def unresolved_environment_reads() -> list[str]:
    return _environment_audit()[1]


def discover_config_root_sections(root: Path = REPOSITORY_ROOT) -> set[str]:
    sections: set[str] = set()
    for path in iter_production_python_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        loaders = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("load_") and "config" in node.name]
        for function in loaders:
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" and isinstance(node.func.value, ast.Name) and node.func.value.id in {"config", "payload"} and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    sections.add(node.args[0].value)
    return sections


def parse_env_example(path: Path = ENV_EXAMPLE_PATH) -> set[str]:
    keys = set()
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


def missing_config_example_sections() -> list[str]:
    required = discover_config_root_sections() - CONFIG_ROOT_ALIASES
    return sorted(required - set(load_mcp_config_example()))


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
        represented = {key for item in servers if isinstance(item, dict) for key in item}
        absent = sorted(set(ServerConfigType.model_fields) - represented)
        if absent:
            missing["servers[*]"] = absent
    for section, model in CONFIG_SECTION_MODELS.items():
        value = payload.get(section)
        if not isinstance(value, dict):
            missing[section] = sorted(model.model_fields)
        else:
            absent = sorted(set(model.model_fields) - set(value))
            if absent:
                missing[section] = absent
    return missing


def validate_config_example_values() -> None:
    payload = load_mcp_config_example()
    servers = payload.get("servers")
    if not isinstance(servers, list) or not servers:
        raise AssertionError("mcp.config.example must contain at least one server")
    for index, item in enumerate(servers):
        try:
            ServerConfigType.model_validate(item)
        except Exception as error:
            raise AssertionError(f"servers[{index}] is invalid: {error}") from error
    for section, model in CONFIG_SECTION_MODELS.items():
        try:
            model.model_validate(payload.get(section))
        except Exception as error:
            raise AssertionError(f"{section} is invalid: {error}") from error


def main() -> int:
    errors = []
    if missing_environment_example_keys(): errors.append("missing environment example keys")
    if unresolved_environment_reads(): errors.append("unresolved environment reads")
    if missing_config_example_sections(): errors.append("missing root config sections")
    if unregistered_config_sections(): errors.append("unregistered root config sections")
    if missing_config_example_fields(): errors.append("missing config fields")
    try:
        validate_config_example_values()
    except AssertionError as error:
        errors.append(str(error))
    if errors:
        print("Configuration example audit failed:")
        for error in errors: print(f"- {error}")
        return 1
    print("Configuration examples cover all discovered parameters.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
