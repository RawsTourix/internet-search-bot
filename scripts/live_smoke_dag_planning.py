"""Live smoke test for v0.4 DAG planning with a real configured LLM.

The runner copies the supplied agent config into a temporary directory, replaces
its MCP server list with a deterministic local smoke server, and writes all
storage into that temporary directory. The source config is never modified.

Examples from the repository root:

    python scripts/live_smoke_dag_planning.py --config src/api/mcp.config

    python scripts/live_smoke_dag_planning.py \
        --config src/api/mcp.config \
        --include-waiting
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Iterable


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from src.core.models import AgentResult, AgentStatus, ClientType
from src.mcp.mcp_client import load_config
from src.mcp.planning_runtime import FinalizingPlanningMCPClient
from src.planning import create_planning_services, load_planning_config
from src.planning.runtime_context import PlanningAwareContentStore
from src.storage import StorageServices, create_storage_services


SMOKE_SERVER_PATH = (
    REPOSITORY_ROOT / "scripts" / "dag_planning_smoke_mcp_server.py"
)


class SmokeFailure(RuntimeError):
    """Raised when the live agent violates a required smoke invariant."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def _event_types(result: AgentResult) -> list[str]:
    return [
        str(event.get("type"))
        for event in result.progress_events
        if event.get("type")
    ]


def _target_tools(result: AgentResult) -> set[str]:
    names = set(result.tools_used)
    for event in result.progress_events:
        for key in ("target_tool_name", "tool_name"):
            value = event.get(key)
            if isinstance(value, str) and value:
                names.add(value)
    return names


def _find_plan_id(result: AgentResult) -> str:
    for event in result.progress_events:
        if event.get("type") != "plan_created":
            continue
        data = event.get("data")
        if isinstance(data, dict):
            value = data.get("plan_id")
            if isinstance(value, str) and value:
                return value
    raise SmokeFailure("plan_created progress event does not expose plan_id")


def _load_plan_metadata(storage_root: Path, plan_id: str) -> dict[str, Any]:
    path = storage_root / "plans" / plan_id / "metadata.json"
    _require(path.is_file(), f"plan metadata was not persisted: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _build_temporary_config(
    *,
    source_config: Path,
    temporary_root: Path,
    temperature: float,
    keep_final_audit: bool,
) -> Path:
    try:
        payload = json.loads(source_config.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise SmokeFailure(f"config file was not found: {source_config}") from error
    except json.JSONDecodeError as error:
        raise SmokeFailure(f"config is not valid JSON: {error}") from error

    if not isinstance(payload, dict):
        raise SmokeFailure("agent config root must be a JSON object")
    if not isinstance(payload.get("llm"), dict):
        raise SmokeFailure("agent config must contain an llm object")

    payload["servers"] = [
        {
            "name": "dag_planning_live_smoke",
            "alias": "smoke",
            "connect_type": "executable",
            "executable": sys.executable,
            "args": [str(SMOKE_SERVER_PATH)],
            "env": {
                "PYTHONUTF8": "1",
                "PYTHONUNBUFFERED": "1",
            },
            "enabled": True,
            "startup_required": True,
        }
    ]

    storage = dict(payload.get("storage") or {})
    storage.update(
        {
            "backend": "filesystem",
            "root_dir": str(temporary_root / "storage"),
            "atomic_writes": True,
            "verify_content_hash": True,
        }
    )
    payload["storage"] = storage

    planning = dict(payload.get("planning") or {})
    planning["enabled"] = True
    payload["planning"] = planning

    llm = dict(payload["llm"])
    llm["temperature"] = temperature
    if not keep_final_audit:
        llm["final_audit"] = False
    payload["llm"] = llm

    output = temporary_root / "mcp.live-smoke.config.json"
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return output


def _build_client(config_path: Path) -> FinalizingPlanningMCPClient:
    (
        server_configs,
        llm_config,
        storage_config,
        memory_config,
        runtime_config,
    ) = load_config(str(config_path))
    planning_config = load_planning_config(str(config_path))

    base_storage_services = create_storage_services(storage_config)
    storage_services = StorageServices(
        config=base_storage_services.config,
        content_store=PlanningAwareContentStore(
            base_storage_services.content_store
        ),
        artifact_store=base_storage_services.artifact_store,
    )
    planning_services = create_planning_services(
        storage_config=storage_config,
        planning_config=planning_config,
    )
    client = FinalizingPlanningMCPClient(
        llm_config,
        storage_services=storage_services,
        memory_config=memory_config,
        runtime_config=runtime_config,
        planning_services=planning_services,
    )
    client._live_smoke_server_configs = server_configs
    return client


async def _run_complex_scenario(
    client: FinalizingPlanningMCPClient,
    *,
    storage_root: Path,
) -> dict[str, Any]:
    session_id = "dag-live-smoke-complex"
    prompt = """
Это контролируемый live-smoke тест DAG-planning.

Задача действительно многоэтапная, поэтому обязательно создай DAG-план:
1. Получи alpha инструментом smoke_get_alpha.
2. Получи beta инструментом smoke_get_beta.
3. Вычисли alpha + beta.
4. Проверь сумму инструментом smoke_verify_total.
5. Заверши все узлы и сам план до финального ответа.

Соблюдай runtime-протокол: перед содержательным MCP-вызовом запусти ready-узел.
Не подменяй вызовы инструментов собственными знаниями.
В финальном ответе обязательно напиши маркер SMOKE_TOTAL=42.
""".strip()

    result = await client.process_query(
        prompt,
        session_id=session_id,
        client_type=ClientType.CLI,
        progress_locale="ru",
    )
    types = _event_types(result)
    targets = _target_tools(result)

    _require(
        result.status == AgentStatus.DONE,
        f"complex scenario status is {result.status.value}: {result.content}",
    )
    _require("plan_created" in types, "complex scenario did not create a plan")
    _require(
        "plan_node_started" in types,
        "complex scenario did not start a plan node",
    )
    _require(
        "plan_node_completed" in types or "plan_completed" in types,
        "complex scenario did not complete plan nodes",
    )
    _require("plan_completed" in types, "complex scenario did not complete plan")

    expected_tools = {
        "smoke_get_alpha",
        "smoke_get_beta",
        "smoke_verify_total",
    }
    missing_tools = expected_tools - targets
    _require(
        not missing_tools,
        f"complex scenario did not call tools: {sorted(missing_tools)}; "
        f"observed={sorted(targets)}",
    )
    _require(
        "42" in result.content,
        f"complex final answer does not contain 42: {result.content!r}",
    )

    plan_id = _find_plan_id(result)
    metadata = _load_plan_metadata(storage_root, plan_id)
    _require(
        metadata.get("status") == "completed",
        f"persisted plan is not completed: {metadata}",
    )

    return {
        "status": result.status.value,
        "iterations": result.iterations,
        "plan_id": plan_id,
        "plan_revision": metadata.get("current_revision"),
        "tools": sorted(expected_tools),
        "final": result.content,
    }


async def _run_simple_scenario(
    client: FinalizingPlanningMCPClient,
) -> dict[str, Any]:
    session_id = "dag-live-smoke-simple"
    result = await client.process_query(
        (
            "Это простой контрольный запрос. Не создавай план и не вызывай "
            "инструменты. Ответь ровно маркером SMOKE_SIMPLE_OK."
        ),
        session_id=session_id,
        client_type=ClientType.CLI,
        progress_locale="ru",
    )
    types = _event_types(result)
    targets = _target_tools(result)

    _require(
        result.status == AgentStatus.DONE,
        f"simple scenario status is {result.status.value}: {result.content}",
    )
    _require("plan_created" not in types, "simple scenario created a plan")
    _require(
        not ({"smoke_get_alpha", "smoke_get_beta", "smoke_verify_total"} & targets),
        f"simple scenario called smoke tools: {sorted(targets)}",
    )
    _require(
        "SMOKE_SIMPLE_OK" in result.content,
        f"simple scenario returned unexpected content: {result.content!r}",
    )

    return {
        "status": result.status.value,
        "iterations": result.iterations,
        "final": result.content,
    }


async def _run_waiting_scenario(
    client: FinalizingPlanningMCPClient,
    *,
    storage_root: Path,
) -> dict[str, Any]:
    session_id = "dag-live-smoke-waiting"
    first = await client.process_query(
        """
Это live-smoke тест WAITING_USER вместе с DAG-планом.

Обязательно создай план. Сначала получи alpha через smoke_get_alpha. До вызова
smoke_get_beta обязательно запроси у пользователя код SMOKE-APPROVED. Перед
вопросом переведи текущий in-progress узел в blocked, как требует runtime.
Не вызывай smoke_get_beta и smoke_verify_total до получения кода.
После кода продолжи план, получи beta, проверь сумму и заверши план.
""".strip(),
        session_id=session_id,
        client_type=ClientType.CLI,
        progress_locale="ru",
    )
    first_types = _event_types(first)
    first_targets = _target_tools(first)

    _require(
        first.status == AgentStatus.WAITING_USER,
        f"waiting scenario did not pause: {first.status.value}: {first.content}",
    )
    _require(first.can_resume, "waiting scenario is not resumable")
    _require("plan_created" in first_types, "waiting scenario did not create plan")
    _require(
        "plan_node_blocked" in first_types,
        "waiting scenario did not explicitly block a node",
    )
    _require(
        "smoke_get_beta" not in first_targets,
        "waiting scenario called beta before approval",
    )

    second = await client.process_query(
        "SMOKE-APPROVED",
        session_id=session_id,
        client_type=ClientType.CLI,
        progress_locale="ru",
    )
    second_types = _event_types(second)
    second_targets = _target_tools(second)

    _require(
        second.status == AgentStatus.DONE,
        f"resumed waiting scenario did not finish: "
        f"{second.status.value}: {second.content}",
    )
    _require(
        "plan_completed" in second_types,
        "resumed waiting scenario did not complete plan",
    )
    _require(
        {"smoke_get_beta", "smoke_verify_total"}.issubset(second_targets),
        f"resumed waiting scenario missed tools: {sorted(second_targets)}",
    )
    _require("42" in second.content, "resumed final answer does not contain 42")

    plan_id = _find_plan_id(first)
    metadata = _load_plan_metadata(storage_root, plan_id)
    _require(
        metadata.get("status") == "completed",
        f"resumed persisted plan is not completed: {metadata}",
    )

    return {
        "first_status": first.status.value,
        "second_status": second.status.value,
        "plan_id": plan_id,
        "plan_revision": metadata.get("current_revision"),
        "final": second.content,
    }


async def _run(args: argparse.Namespace) -> int:
    source_config = Path(args.config).expanduser().resolve()
    if not SMOKE_SERVER_PATH.is_file():
        raise SmokeFailure(f"smoke MCP server is missing: {SMOKE_SERVER_PATH}")

    temporary_root = Path(
        tempfile.mkdtemp(prefix="internet-search-bot-dag-smoke-")
    ).resolve()
    client: FinalizingPlanningMCPClient | None = None
    try:
        temporary_config = _build_temporary_config(
            source_config=source_config,
            temporary_root=temporary_root,
            temperature=args.temperature,
            keep_final_audit=args.keep_final_audit,
        )
        client = _build_client(temporary_config)
        await client.connect_to_servers(client._live_smoke_server_configs)

        report: dict[str, Any] = {
            "complex": await _run_complex_scenario(
                client,
                storage_root=temporary_root / "storage",
            ),
            "simple": await _run_simple_scenario(client),
        }
        if args.include_waiting:
            report["waiting"] = await _run_waiting_scenario(
                client,
                storage_root=temporary_root / "storage",
            )

        print("\nDAG PLANNING LIVE SMOKE: PASS")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if args.keep_temp:
            print(f"Temporary data kept at: {temporary_root}")
        return 0
    finally:
        if client is not None:
            await client.cleanup()
        if not args.keep_temp:
            shutil.rmtree(temporary_root, ignore_errors=True)


def _parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run v0.4 DAG-planning smoke scenarios against a real LLM."
    )
    parser.add_argument(
        "--config",
        default=os.getenv("AGENT_CONFIG_PATH", ""),
        help="Path to the real agent JSON config. Defaults to AGENT_CONFIG_PATH.",
    )
    parser.add_argument(
        "--include-waiting",
        action="store_true",
        help="Also test explicit block → WAITING_USER → resume.",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.1,
        help="Temporary LLM temperature used only by the smoke config.",
    )
    parser.add_argument(
        "--keep-final-audit",
        action="store_true",
        help="Keep final_audit from the source config instead of disabling it.",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="Keep temporary config, plans and content after the run.",
    )
    args = parser.parse_args(argv)
    if not args.config:
        parser.error("--config is required when AGENT_CONFIG_PATH is empty")
    if not 0 <= args.temperature <= 2:
        parser.error("--temperature must be between 0 and 2")
    return args


def main() -> int:
    args = _parse_args()
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\nDAG PLANNING LIVE SMOKE: INTERRUPTED", file=sys.stderr)
        return 130
    except Exception as error:
        print("\nDAG PLANNING LIVE SMOKE: FAIL", file=sys.stderr)
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        if args.keep_temp:
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
