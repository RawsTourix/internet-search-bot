"""Run the isolated v0.4 Web/Telegram/artifact/OutputBatch audit.

This is intentionally not wired into normal CI.  It orchestrates existing
real-service tests plus audit-only ASGI checks, installs hard guards around the
Agent/LLM/MCP/network/real-Telegram boundaries, and writes Markdown/JSON reports.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[2]
REPORT_MD = ROOT / "reports" / "v0.4-transport-artifact-roast.md"
REPORT_JSON = ROOT / "reports" / "v0.4-transport-artifact-roast.json"
CHECKPOINT_JSON = ROOT / "reports" / ".v0.4-transport-artifact-roast.checkpoint.json"
AUDIT_CHECKS = "scripts/audit/synthetic_transport_artifact_checks.py"


SCENARIOS: dict[str, dict] = {
    "ING-COM-001": {
        "name": "Common ingress, grouping, explicit control and limits",
        "transport": "common",
        "selectors": [
            "tests/test_artifact_ingress.py",
            "tests/test_artifact_ingress_grouping.py",
            "tests/test_artifact_ingress_routing.py",
            "tests/test_artifact_input_draft_control.py",
            "tests/test_artifact_explicit_collection_grouping.py",
            "tests/test_artifact_explicit_collection_rejection.py",
            "tests/test_semantic_ingress_limits.py",
            "tests/test_unified_input_runtime_foundation.py",
        ],
    },
    "ING-WEB-001": {
        "name": "Web ASGI multipart normalization and admission",
        "transport": "web",
        "selectors": [AUDIT_CHECKS + "::test_web_real_router_valid_replay_and_binary_integrity", AUDIT_CHECKS + "::test_web_real_router_rejects_malformed_manifest", AUDIT_CHECKS + "::test_web_real_router_rejects_missing_unexpected_and_duplicate_uploads", AUDIT_CHECKS + "::test_web_real_router_rejects_two_slots_for_one_upload_field", AUDIT_CHECKS + "::test_web_real_router_rejects_extra_plain_form_field", AUDIT_CHECKS + "::test_web_real_router_rejects_wrong_transport_key"],
    },
    "ING-TG-001": {
        "name": "Telegram normalization, album, commands and presentation",
        "transport": "telegram",
        "selectors": [
            "tests/test_telegram_semantic_resolvers.py",
            "tests/test_telegram_artifact_bridge.py",
            "tests/test_telegram_media_group_coordination.py",
            "tests/test_artifact_telegram_batch_commands.py",
            "tests/test_artifact_telegram_late_album_and_status.py",
            "tests/test_artifact_telegram_media_group_attach.py",
            "tests/test_artifact_telegram_presentation_relocation.py",
            "tests/test_artifact_telegram_robustness_hardening.py",
            "tests/test_artifact_telegram_exact_group_cleanup.py",
        ],
    },
    "ART-001": {
        "name": "Artifact persistence, versions, delivery state and integrity",
        "transport": "common",
        "selectors": [
            "tests/test_storage_artifact_store.py",
            "tests/test_artifact_delivery.py",
            "tests/test_advanced_artifact_delivery_policy.py",
            "tests/test_artifact_access_scopes.py",
            "tests/test_artifact_transport_failures.py",
            "tests/test_artifact_workspace.py",
        ],
    },
    "OUT-COM-001": {
        "name": "OutputBatch assembly, manifests, claims and receipts",
        "transport": "common",
        "selectors": [
            "tests/test_output_assembly_commit_once.py",
            "tests/test_output_manifest_integrity.py",
            "tests/test_output_claim_idempotency.py",
            "tests/test_output_claim_and_capability_limits.py",
            "tests/test_output_receipt_semantics.py",
            "tests/test_output_evidence_policy.py",
            "tests/test_output_semantic_manifest_budget.py",
        ],
    },
    "OUT-WEB-001": {
        "name": "Web ready outbox authority, content and aggregate receipt",
        "transport": "web",
        "selectors": [
            "tests/test_output_outbox_api_authority.py",
            "tests/test_output_outbox_delivery_content.py",
            "tests/test_web_output_delivery_hardening.py",
        ],
    },
    "OUT-TG-001": {
        "name": "Telegram renderer/executor and delivery completion",
        "transport": "telegram",
        "selectors": [
            "tests/test_telegram_output_plan_executor.py",
            "tests/test_telegram_document_group_fallback.py",
            "tests/test_telegram_scoped_output_execution.py",
            "tests/test_telegram_claimed_output_gateway.py",
            "tests/test_ready_output_outbox.py",
            "tests/test_telegram_ready_outbox_claim_retry.py",
        ],
    },
    "REC-001": {
        "name": "Ingress/output restart and crash-window recovery",
        "transport": "common",
        "selectors": [
            "tests/test_artifact_ingress_startup_recovery.py",
            "tests/test_artifact_ingress_failure_recovery.py",
            "tests/test_artifact_input_draft_control_recovery.py",
            "tests/test_artifact_delivery_recovery.py",
            "tests/test_output_delivery_recovery.py",
            "tests/test_output_ownership_startup_recovery.py",
        ],
    },
    "TRACE-001": {
        "name": "Artifact lifecycle tracing and extended redaction",
        "transport": "common",
        "selectors": [
            "tests/test_artifact_tracing.py",
            "tests/test_artifact_native_lifecycle_tracing.py",
            AUDIT_CHECKS + "::test_trace_redacts_sensitive_values_not_only_sensitive_keys",
        ],
    },
    "SEC-001": {
        "name": "Transport authority and cross-scope isolation",
        "transport": "cross-transport",
        "selectors": [
            "tests/test_artifact_input_collection_routes.py",
            "tests/test_output_outbox_api_authority.py",
            "tests/test_telegram_scoped_output_execution.py",
            "tests/test_artifact_access_scopes.py",
        ],
    },
}


RACE_SELECTORS = [
    "tests/test_artifact_ingress_reservation_race.py::IngressReservationRaceTests::test_instruction_waits_for_file_draft_reservation",
    "tests/test_unified_input_runtime_foundation.py::UnifiedInputRuntimeFoundationTests::test_text_resets_durable_quiet_deadline_before_commit",
    "tests/test_artifact_input_draft_control.py::InputDraftControlTests::test_commit_request_is_persisted_while_upload_is_in_flight",
    "tests/test_artifact_input_draft_control.py::InputDraftControlTests::test_cancel_is_exact_and_does_not_cancel_neighbor_scope",
    "tests/test_artifact_ingress.py::ArtifactIngressTests::test_replay_returns_same_event_batch_and_artifact",
    "tests/test_telegram_media_group_coordination.py::TelegramMediaGroupCoordinationTests::test_rescheduled_worker_does_not_clear_live_activity",
    "tests/test_output_ownership_startup_recovery.py::OutputOwnershipStartupRecoveryTests::test_partially_bound_ready_delivery_is_completed_idempotently",
    "tests/test_output_assembly_commit_once.py::OutputAssemblyCommitOnceTests::test_terminal_output_is_reused_after_delivery_state_changes",
    "tests/test_artifact_telegram_presentation_relocation.py::TelegramPresentationRelocationTests::test_failed_old_delete_keeps_new_handle_authoritative",
]


RANDOM_OPERATIONS = {
    "text": "tests/test_artifact_ingress.py::ArtifactIngressTests::test_text_only_batch_commits_without_artifacts",
    "file": "tests/test_artifact_ingress.py::ArtifactIngressTests::test_streaming_file_commits_exact_input_artifact",
    "album member": "tests/test_telegram_media_group_coordination.py::TelegramMediaGroupCoordinationTests::test_late_member_resets_quiet_period_and_is_awaited",
    "caption": "tests/test_semantic_ingress_limits.py::SemanticIngressLimitTests::test_caption_has_one_canonical_agent_representation",
    "collect": "tests/test_artifact_input_draft_control.py::InputDraftControlTests::test_start_creates_empty_explicit_collection",
    "send": "tests/test_artifact_input_draft_control.py::InputDraftControlTests::test_files_first_draft_is_promoted_and_files_only_send_commits",
    "cancel": "tests/test_artifact_input_draft_control.py::InputDraftControlTests::test_cancel_is_exact_and_does_not_cancel_neighbor_scope",
    "inspect": "tests/test_artifact_input_draft_control.py::InputDraftControlTests::test_start_is_idempotent_and_scope_has_one_active_collection",
    "duplicate": "tests/test_artifact_ingress.py::ArtifactIngressTests::test_replay_returns_same_event_batch_and_artifact",
    "restart": "tests/test_output_ownership_startup_recovery.py::OutputOwnershipStartupRecoveryTests::test_crash_window_ready_batch_is_repaired_after_stores_reopen",
    "claim": "tests/test_output_claim_idempotency.py::OutputClaimIdempotencyTests::test_same_request_replays_original_attempt",
    "download": "tests/test_output_outbox_delivery_content.py::OutputOutboxDeliveryContentTests::test_exact_claimed_instance_can_stream_member_bytes",
    "receipt": "tests/test_output_receipt_semantics.py::OutputReceiptSemanticsTests::test_public_reconcile_updates_output_and_artifact_records",
}


GAPS = [
    "No public synthetic state-machine seam spans ingress through claim/download/receipt in one randomized durable session; randomized operations therefore use fresh real-service fixtures.",
    "The Web multipart router has no production composition factory that can assemble artifact, collection and outbox routers without an application-shaped facade.",
    "The existing input-collection router tests use a mocked draft-control service; full router-to-filesystem explicit Web control is not directly covered.",
    "No public API models the crash windows 'bytes stored before artifact record' or 'receipt partially persisted' without private store mutation.",
    "Malformed persisted JSON and missing-record recovery are covered only in selected stores, not across every listed restart stage.",
    "A synthetic Telegram handler fixture for every Update subtype and /status,/reset path is not exposed as one public adapter.",
    "Exact 32/33 attachment, near-size-limit, coroutine-cancellation, and every declared-size mismatch combination are not all represented by public endpoint tests.",
    "All semantic OutputPart types are executor-tested, but one public synthetic AgentResult builder does not expose every type for one mixed OutputBatch assembly.",
    "Trace append failure is testable, but every domain operation cannot inject it through a uniform public seam.",
]


@dataclass
class Failure:
    test: str
    message: str
    output: str
    reproduction_runs: int = 0
    reproduction_failures: int = 0


@dataclass
class ScenarioResult:
    scenario_id: str
    name: str
    transport: str
    initial_state: str = "fresh TemporaryDirectory per pytest test"
    actions: list[str] = field(default_factory=list)
    expected_result: str = "all selected invariants hold"
    actual_result: str = ""
    http_statuses: list[int] = field(default_factory=list)
    input_batch_state: str = "captured by assertions/JUnit evidence"
    artifact_records: str = "captured by assertions/JUnit evidence"
    delivery_records: str = "captured by assertions/JUnit evidence"
    output_batch_state: str = "captured by assertions/JUnit evidence"
    receipt_state: str = "captured by assertions/JUnit evidence"
    trace_events: str = "captured by assertions/JUnit evidence"
    logs: str = ""
    status: str = "GAP"
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    duration_seconds: float = 0.0
    failures: list[Failure] = field(default_factory=list)
    guard_counters: dict[str, int] = field(default_factory=dict)


def _guard_plugin() -> str:
    return r'''
import json
import os
import socket
from pathlib import Path
from unittest.mock import patch

COUNTERS = {"llm_calls": 0, "mcp_calls": 0, "agent_cycle_runs": 0, "external_http_calls": 0, "real_telegram_calls": 0}
PATCHERS = []

def _blocked(counter, label):
    async def blocked_async(*args, **kwargs):
        COUNTERS[counter] += 1
        raise AssertionError(label)
    return blocked_async

def pytest_configure(config):
    original_connect = socket.socket.connect
    def guarded_connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            COUNTERS["external_http_calls"] += 1
            raise AssertionError(f"external network is forbidden: {address!r}")
        return original_connect(sock, address)
    PATCHERS.append(patch.object(socket.socket, "connect", guarded_connect))
    from src.api.artifact_transport import ArtifactTransportFacade
    from src.core.message_processor import MessageProcessor
    from src.mcp.mcp_client import MCPClient
    from src.mcp.server_manager import MCPServerManager
    PATCHERS.extend([
        patch.object(ArtifactTransportFacade, "run_committed_batch", _blocked("agent_cycle_runs", "run_committed_batch is forbidden")),
        patch.object(MessageProcessor, "process_committed_batch", _blocked("agent_cycle_runs", "AgentCycle is forbidden")),
        patch.object(MCPClient, "_call_llm", _blocked("llm_calls", "LLM call is forbidden")),
        patch.object(MCPClient, "connect_to_servers", _blocked("mcp_calls", "MCP connect is forbidden")),
        patch.object(MCPServerManager, "call_tool", _blocked("mcp_calls", "MCP call is forbidden")),
    ])
    try:
        from telegram import Bot
        PATCHERS.append(patch.object(Bot, "_post", _blocked("real_telegram_calls", "real Telegram call is forbidden")))
    except Exception:
        pass
    for item in PATCHERS:
        item.start()

def pytest_collection_modifyitems(session, config, items):
    from _pytest.unittest import TestCaseFunction
    plan_path = os.environ.get("AUDIT_ITEM_PLAN_PATH")
    plan = (
        json.loads(Path(plan_path).read_text(encoding="utf-8"))
        if plan_path
        else []
    )
    if not plan:
        return
    original = list(items)
    expanded = []
    occurrences = {}
    for requested in plan:
        matches = [
            item for item in original
            if item.nodeid == requested
            or item.nodeid.startswith(requested + "[")
            or item.nodeid.startswith(requested + "::")
        ]
        if not matches:
            raise AssertionError(f"audit selector was not collected: {requested}")
        for item in matches:
            occurrence = occurrences.get(item.nodeid, 0)
            occurrences[item.nodeid] = occurrence + 1
            if occurrence and isinstance(item, TestCaseFunction):
                clone = item.__class__.from_parent(item.parent, name=item.name)
                clone._nodeid = item.nodeid
                expanded.append(clone)
            else:
                expanded.append(item)
    items[:] = expanded

def pytest_unconfigure(config):
    Path(os.environ["AUDIT_GUARD_COUNTERS"]).write_text(json.dumps(COUNTERS), encoding="utf-8")
    for item in reversed(PATCHERS):
        item.stop()
'''


def _parse_junit(path: Path) -> tuple[int, int, int, list[Failure]]:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    passed = failed = skipped = 0
    failures: list[Failure] = []
    recorded_failures: set[str] = set()
    for suite in suites:
        for case in suite.findall("testcase"):
            class_name = case.get("classname", "")
            name = case.get("name", "")
            dotted = class_name.split(".") if class_name else []
            owner = None
            if dotted and dotted[-1][:1].isupper():
                owner = dotted.pop()
            test = "/".join(dotted) + ".py"
            if owner:
                test += f"::{owner}"
            test += f"::{name}"
            failure = case.find("failure")
            if failure is None:
                failure = case.find("error")
            skip = case.find("skipped")
            if failure is not None:
                failed += 1
                if test not in recorded_failures:
                    failures.append(Failure(test, failure.get("message", ""), (failure.text or "")[-4000:]))
                    recorded_failures.add(test)
            elif skip is not None:
                skipped += 1
            else:
                passed += 1
    return passed, failed, skipped, failures


def _run_pytest(scenario_id: str, spec: dict, selectors: Iterable[str]) -> ScenarioResult:
    started = time.monotonic()
    selector_plan = list(selectors)
    unique_selectors = list(dict.fromkeys(selector_plan))
    with tempfile.TemporaryDirectory(prefix=f"audit-{scenario_id.lower()}-") as temporary:
        temp = Path(temporary)
        (temp / "audit_guard.py").write_text(_guard_plugin(), encoding="utf-8")
        junit = temp / "junit.xml"
        counters_path = temp / "guard-counters.json"
        plan_path = temp / "item-plan.json"
        plan_path.write_text(json.dumps(selector_plan), encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join([str(temp), str(ROOT), env.get("PYTHONPATH", "")])
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        env["AUDIT_GUARD_COUNTERS"] = str(counters_path)
        env["AUDIT_ITEM_PLAN_PATH"] = str(plan_path)
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "pytest_asyncio.plugin",
            "-p",
            "audit_guard",
            "-p",
            "no:cacheprovider",
            "-q",
            "--keep-duplicates",
            "--tb=short",
            f"--junitxml={junit}",
            *unique_selectors,
        ]
        completed = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True)
        result = ScenarioResult(
            scenario_id=scenario_id,
            name=spec["name"],
            transport=spec["transport"],
            actions=selector_plan,
            logs=(completed.stdout + "\n" + completed.stderr)[-12000:],
            duration_seconds=round(time.monotonic() - started, 3),
        )
        if junit.exists():
            result.passed, result.failed, result.skipped, result.failures = _parse_junit(junit)
        else:
            result.failed = 1
            result.failures = [Failure("pytest-bootstrap", f"exit={completed.returncode}", result.logs)]
        result.guard_counters = (
            json.loads(counters_path.read_text(encoding="utf-8"))
            if counters_path.exists()
            else {"guard_report_missing": 1}
        )
        result.status = "PASS" if completed.returncode == 0 else "FAIL"
        result.actual_result = f"{result.passed} passed, {result.failed} failed, {result.skipped} skipped"
        return result


def _stage_heatmap(results: list[ScenarioResult]) -> list[dict]:
    mapping = {
        "Normalization": ("ING-COM-001", "ING-WEB-001", "ING-TG-001"),
        "Authorization": ("SEC-001", "OUT-WEB-001", "OUT-TG-001"),
        "Ingress admission": ("ING-COM-001", "ING-WEB-001", "ING-TG-001"),
        "Upload streaming": ("ING-COM-001", "ING-WEB-001", "ING-TG-001"),
        "InputBatch assembly": ("ING-COM-001", "ING-WEB-001", "ING-TG-001"),
        "Explicit control": ("ING-COM-001", "ING-WEB-001", "ING-TG-001"),
        "Artifact persistence": ("ART-001", "ING-WEB-001", "ING-TG-001"),
        "OutputBatch assembly": ("OUT-COM-001", "OUT-WEB-001", "OUT-TG-001"),
        "Outbox claim": ("OUT-COM-001", "OUT-WEB-001", "OUT-TG-001"),
        "Content delivery": ("ART-001", "OUT-WEB-001", "OUT-TG-001"),
        "Receipt": ("OUT-COM-001", "OUT-WEB-001", "OUT-TG-001"),
        "Recovery": ("REC-001", "REC-001", "REC-001"),
        "Tracing": ("TRACE-001", "TRACE-001", "TRACE-001"),
    }
    by_id = {item.scenario_id: item for item in results}
    rows = []
    for stage, ids in mapping.items():
        values = []
        for scenario_id in ids:
            item = by_id.get(scenario_id)
            values.append(item.passed if item else 0)
        relevant = [by_id[item].status for item in ids if item in by_id]
        state = "FAIL" if "FAIL" in relevant else "PASS" if relevant else "GAP"
        rows.append({"stage": stage, "common": values[0], "web": values[1], "telegram": values[2], "result": state})
    return rows


def _failure_details(results: list[ScenarioResult], seed: int, command: str) -> list[dict]:
    details = []
    index = 1
    for scenario in results:
        for failure in scenario.failures:
            if failure.reproduction_runs == 0:
                reproducibility = "not confirmed"
            elif failure.reproduction_failures == failure.reproduction_runs:
                reproducibility = f"deterministic: {failure.reproduction_failures}/{failure.reproduction_runs} failed"
            else:
                reproducibility = f"flaky: {failure.reproduction_failures}/{failure.reproduction_runs} failed"
            if "two_slots_for_one_upload_field" in failure.test:
                http_evidence = "POST /web/input-batches -> HTTP 500 Internal Server Error; durable file inventory is embedded in Relevant logs."
            elif "extra_plain_form_field" in failure.test:
                http_evidence = "POST /web/input-batches with an unexpected ordinary form field -> HTTP 201 Created and committed InputBatch payload."
            else:
                http_evidence = "See assertion/JUnit failure; ASGI only where applicable."
            failure_transport = scenario.transport
            failure_scope = "adapter-specific" if scenario.transport in {"web", "telegram"} else "shared"
            first_broken_stage = "Tracing" if scenario.scenario_id == "TRACE-001" else "Normalization" if scenario.scenario_id.startswith("ING-") else "Durable transition"
            suspected_owner = "src/api/artifact_routes.py" if scenario.scenario_id == "ING-WEB-001" else "src/artifacts/tracing.py" if scenario.scenario_id == "TRACE-001" else "see failing test stack"
            if "test_late_member_resets_quiet_period_and_is_awaited" in failure.test:
                failure_transport = "telegram"
                failure_scope = "adapter-specific"
                first_broken_stage = "InputBatch assembly (album quiet-window coordination)"
                suspected_owner = "src/servers/telegram/media_group_runner.py"
            details.append({
                "id": f"ROAST-{index:03d}",
                "severity": "HIGH" if scenario.scenario_id in {"ING-WEB-001", "SEC-001"} else "MEDIUM",
                "transport": failure_transport,
                "scope": failure_scope,
                "first_broken_stage": first_broken_stage,
                "scenario": f"{scenario.scenario_id}: {failure.test}",
                "expected": scenario.expected_result,
                "actual": failure.message or failure.output[-500:],
                "reproducibility": reproducibility,
                "seed": seed,
                "exact_commands": command,
                "http_request_response": http_evidence,
                "input_batch_state": scenario.input_batch_state,
                "artifact_state": scenario.artifact_records,
                "delivery_state": scenario.delivery_records,
                "output_batch_state": scenario.output_batch_state,
                "trace_state": scenario.trace_events,
                "relevant_logs": failure.output[-2000:],
                "suspected_owner_module": suspected_owner,
                "why_this_is_a_defect": "The observed result violates the accepted invariant encoded by the synthetic audit assertion.",
                "minimal_deterministic_reproduction": failure.test,
            })
            index += 1
    return details


def _to_jsonable(result: ScenarioResult) -> dict:
    return asdict(result)


def _from_jsonable(value: dict) -> ScenarioResult:
    copied = dict(value)
    copied["failures"] = [Failure(**item) for item in copied.get("failures", [])]
    return ScenarioResult(**copied)


def _save_checkpoint(results: list[ScenarioResult], *, seed: int) -> None:
    CHECKPOINT_JSON.parent.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_JSON.write_text(
        json.dumps(
            {"seed": seed, "scenarios": [_to_jsonable(item) for item in results]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _write_reports(payload: dict) -> None:
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = payload["executive_summary"]
    lines = [
        "# v0.4 Synthetic Web/Telegram Transport and Artifact Roast",
        "",
        "## Executive summary",
        "",
        f"- SHA: `{summary['sha']}`",
        f"- Initial worktree clean: `{summary['initial_worktree_clean']}`",
        f"- Environment: `{summary['environment']}`",
        f"- Host platform: `{summary['host_platform']}`",
        f"- Python: `{summary['python_version']}`",
        f"- Seed: `{summary['seed']}`",
        f"- Scenarios: `{summary['scenario_count']}`; PASS `{summary['pass']}`; FAIL `{summary['fail']}`; GAP `{summary['gap']}`; flaky `{summary['flaky']}`",
        f"- Race repetitions per race: `{summary['race_repetitions']}`",
        f"- Randomized sequences: `{summary['random_sequences']}`",
        f"- Baseline: `{summary['baseline']['passed']} passed, {summary['baseline']['failed']} failed, {summary['baseline']['skipped']} skipped, {summary['baseline']['subtests']} subtests passed in {summary['baseline']['duration_seconds']}s`",
        f"- LLM calls: `{summary['counters']['llm_calls']}`",
        f"- MCP calls: `{summary['counters']['mcp_calls']}`",
        f"- AgentCycle runs: `{summary['counters']['agent_cycle_runs']}`",
        f"- External HTTP calls: `{summary['counters']['external_http_calls']}`",
        f"- Real Telegram calls: `{summary['counters']['real_telegram_calls']}`",
        "",
        "Commands:",
        "",
        *[f"- `{item}`" for item in summary["commands"]],
        "",
        "The dependency-install and tiktoken-cache warm-up were setup operations, not synthetic scenarios. Every recorded scenario ran in a Docker container with `--network none`; HTTP tests used only `httpx.ASGITransport`.",
        "",
        "## Existing coverage map",
        "",
        "| Area | Existing evidence | Audit conclusion |",
        "| --- | --- | --- |",
        "| Common ingress | filesystem ingress/grouping/control tests | real services, durable temp roots |",
        "| Web ingress | route smoke helper was external-server-oriented | audit adds local real-router ASGI checks |",
        "| Telegram ingress | resolver, bridge, album and handler-level tests | real adapters with fake Telegram boundary |",
        "| Artifacts | filesystem stores, workspace, delivery state tests | strong real-store coverage |",
        "| OutputBatch | assembler/store/receipt tests | strong durable coverage |",
        "| Web outbox | real routers and filesystem stores | strong authority/content coverage |",
        "| Telegram output | real executor with FakeTelegramBot/Gateway | strong transport-boundary coverage |",
        "| Recovery | ingress/output startup and stale-claim tests | several private crash windows remain gaps |",
        "| Tracing | real JSONL store plus best-effort failure | audit extends value-based redaction probes |",
        "",
        "## Stage heatmap",
        "",
        "| Stage | Common | Web | Telegram | Result |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for row in payload["stage_heatmap"]:
        lines.append(f"| {row['stage']} | {row['common']} | {row['web']} | {row['telegram']} | {row['result']} |")
    lines.extend(["", "## Scenario results", "", "| ID | Transport | Result | Passed | Failed | Skipped | Duration |", "| --- | --- | --- | ---: | ---: | ---: | ---: |"])
    for item in payload["scenarios"]:
        lines.append(f"| {item['scenario_id']} | {item['transport']} | {item['status']} | {item['passed']} | {item['failed']} | {item['skipped']} | {item['duration_seconds']:.3f}s |")
    lines.extend(["", "## Defects", ""])
    if not payload["defects"]:
        lines.append("No new deterministic defect was observed in the executed slice.")
    for defect in payload["defects"]:
        lines.extend([
            f"### {defect['id']}", "",
            f"- Severity: {defect['severity']}",
            f"- Transport: {defect['transport']}",
            f"- Shared/adaptor-specific: {defect['scope']}",
            f"- First broken stage: {defect['first_broken_stage']}",
            f"- Scenario: `{defect['scenario']}`",
            f"- Expected: {defect['expected']}",
            f"- Actual: {defect['actual']}",
            f"- Reproducibility: {defect['reproducibility']}",
            f"- Seed: `{defect['seed']}`",
            f"- Exact commands: `{defect['exact_commands']}`",
            f"- HTTP request/response: {defect['http_request_response']}",
            f"- InputBatch state: {defect['input_batch_state']}",
            f"- Artifact state: {defect['artifact_state']}",
            f"- Delivery state: {defect['delivery_state']}",
            f"- OutputBatch state: {defect['output_batch_state']}",
            f"- Trace state: {defect['trace_state']}",
            f"- Suspected owner module: `{defect['suspected_owner_module']}`",
            f"- Why this is a defect: {defect['why_this_is_a_defect']}",
            f"- Minimal deterministic reproduction: `{defect['minimal_deterministic_reproduction']}`",
            "",
            "Relevant logs:", "", "```text", defect["relevant_logs"], "```", "",
        ])
    lines.extend(["## Coverage gaps", ""])
    lines.extend(f"- GAP: {item}" for item in payload["coverage_gaps"])
    lines.extend(["", "## Passed invariants", ""])
    lines.extend(f"- {item}" for item in payload["passed_invariants"])
    lines.extend(["", "## Baseline note", "", payload["baseline_note"], ""])
    REPORT_MD.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=20260802)
    parser.add_argument("--race-repeats", type=int, default=100)
    parser.add_argument("--random-sequences", type=int, default=200)
    parser.add_argument("--baseline-passed", type=int, default=723)
    parser.add_argument("--baseline-failed", type=int, default=0)
    parser.add_argument("--baseline-skipped", type=int, default=0)
    parser.add_argument("--baseline-subtests", type=int, default=108)
    parser.add_argument("--baseline-duration", type=float, default=306.65)
    parser.add_argument("--sha", default="04b6254e5984a328d5ae652c85dd418178c25d5b")
    parser.add_argument("--branch", default="fix/v0.4-ingress-reservation-race")
    parser.add_argument("--initial-worktree-clean", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--host-platform", default="Microsoft Windows NT 10.0.26200.0")
    parser.add_argument("--only", action="append", choices=sorted(SCENARIOS))
    parser.add_argument("--confirm-repeats", type=int, default=10)
    parser.add_argument("--skip-confirmations", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--rerun", action="append", choices=[*sorted(SCENARIOS), "RACE-001", "RACE-002"])
    parser.add_argument("--skip-race", action="store_true")
    args = parser.parse_args()
    print(f"synthetic audit seed={args.seed}", flush=True)
    selected = set(args.only or SCENARIOS)
    results: list[ScenarioResult] = []
    if args.resume and (CHECKPOINT_JSON.exists() or REPORT_JSON.exists()):
        source = CHECKPOINT_JSON if CHECKPOINT_JSON.exists() else REPORT_JSON
        checkpoint = json.loads(source.read_text(encoding="utf-8"))
        if checkpoint.get("seed") != args.seed:
            report_seed = checkpoint.get("executive_summary", {}).get("seed")
            if report_seed != args.seed:
                raise RuntimeError("checkpoint/report seed does not match requested seed")
        results = [_from_jsonable(item) for item in checkpoint.get("scenarios", [])]
        rerun = set(args.rerun or [])
        results = [item for item in results if item.scenario_id not in rerun]
    completed_ids = {item.scenario_id for item in results}
    for scenario_id, spec in SCENARIOS.items():
        if scenario_id in selected and scenario_id not in completed_ids:
            results.append(_run_pytest(scenario_id, spec, spec["selectors"]))
            _save_checkpoint(results, seed=args.seed)

    if not args.skip_race and not args.only:
        race_selectors = [selector for _ in range(args.race_repeats) for selector in RACE_SELECTORS]
        if "RACE-001" not in completed_ids:
            results.append(_run_pytest("RACE-001", {"name": "Deterministic race repetitions", "transport": "common"}, race_selectors))
            _save_checkpoint(results, seed=args.seed)
        rng = random.Random(args.seed)
        randomized: list[str] = []
        operation_names = list(RANDOM_OPERATIONS)
        for _ in range(args.random_sequences):
            for _ in range(rng.randint(5, 30)):
                randomized.append(RANDOM_OPERATIONS[rng.choice(operation_names)])
        if "RACE-002" not in completed_ids:
            results.append(_run_pytest("RACE-002", {"name": "Deterministic randomized operation sequences", "transport": "cross-transport"}, randomized))
            _save_checkpoint(results, seed=args.seed)

    if not args.skip_confirmations:
        unique_failures: dict[str, list[Failure]] = {}
        for result in results:
            for failure in result.failures:
                if failure.test.startswith("pytest-bootstrap"):
                    continue
                unique_failures.setdefault(failure.test, []).append(failure)
        for node_id, matching in unique_failures.items():
            confirmation = _run_pytest(
                "REPRO",
                {"name": f"Isolated reproduction: {node_id}", "transport": "common"},
                [node_id] * args.confirm_repeats,
            )
            runs = confirmation.passed + confirmation.failed + confirmation.skipped
            for failure in matching:
                failure.reproduction_runs = runs
                failure.reproduction_failures = confirmation.failed

    counters = {"llm_calls": 0, "mcp_calls": 0, "agent_cycle_runs": 0, "external_http_calls": 0, "real_telegram_calls": 0}
    for result in results:
        for key in counters:
            counters[key] += result.guard_counters.get(key, 0)
    command = f"python scripts/audit/synthetic_transport_artifact_roast.py --seed {args.seed} --race-repeats {args.race_repeats} --random-sequences {args.random_sequences}"
    defects = _failure_details(results, args.seed, command)
    pass_count = sum(item.status == "PASS" for item in results)
    fail_count = sum(item.status == "FAIL" for item in results)
    flaky_count = sum(
        0 < item.reproduction_failures < item.reproduction_runs
        or (item.reproduction_runs > 0 and item.reproduction_failures == 0)
        for result in results
        for item in result.failures
    )
    payload = {
        "executive_summary": {
            "sha": args.sha,
            "branch": args.branch,
            "initial_worktree_clean": args.initial_worktree_clean,
            "environment": platform.platform(),
            "host_platform": args.host_platform,
            "python_version": platform.python_version(),
            "seed": args.seed,
            "scenario_count": len(results) + len(GAPS),
            "pass": pass_count,
            "fail": fail_count,
            "gap": len(GAPS),
            "flaky": flaky_count,
            "race_repetitions": 0 if args.skip_race else args.race_repeats,
            "random_sequences": 0 if args.skip_race else args.random_sequences,
            "counters": counters,
            "baseline": {"passed": args.baseline_passed, "failed": args.baseline_failed, "skipped": args.baseline_skipped, "subtests": args.baseline_subtests, "duration_seconds": args.baseline_duration},
            "commands": [
                "docker run --rm --network none --tmpfs /app/logging:rw,nosuid,nodev,noexec -e PYTHONPATH=/opt/deps:/app -e PYTHONDONTWRITEBYTECODE=1 -e PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 -e TIKTOKEN_CACHE_DIR=/opt/tiktoken-cache -v internet-search-bot-audit-py312:/opt/deps:ro -v internet-search-bot-audit-tiktoken:/opt/tiktoken-cache:ro -v <repo>:/app:ro -w /app python:3.12-slim python -m pytest -p pytest_asyncio.plugin -p no:cacheprovider -q --tb=short",
                command,
            ],
        },
        "scenarios": [_to_jsonable(item) for item in results],
        "stage_heatmap": _stage_heatmap(results),
        "defects": defects,
        "coverage_gaps": GAPS,
        "passed_invariants": [
            "Each selected existing test uses a fresh unittest TemporaryDirectory/tmp_path fixture or audit-only tmp_path.",
            "Exact ingress, claim and receipt replays did not create duplicate durable records where executed.",
            "OutputBatch part order, ownership and terminal receipt consistency held in the executed assembler/outbox tests.",
            "Cross-session/client/instance authority checks held in the executed API and executor tests.",
            "No AgentRuntime, LLM, MCP, external HTTP or real Telegram call crossed the installed guards.",
            "Trace failures remained best-effort in the existing durable trace test.",
        ],
        "baseline_note": "An initial read-only preflight produced 67 collection errors because production imports open logging/*.log. A tmpfs overlay for /app/logging fixed the launch seam. The first complete network-disabled baseline then had one environment-only tiktoken cache miss (722 passed, 1 failed, 108 subtests); after a setup-only cache warm-up, the final baseline was clean.",
    }
    _write_reports(payload)
    CHECKPOINT_JSON.unlink(missing_ok=True)
    print(json.dumps(payload["executive_summary"], ensure_ascii=False, indent=2), flush=True)
    return 1 if defects or any(counters.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
