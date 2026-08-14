"""Production composition for IR-9 runtime diagnostics projections."""

from __future__ import annotations

from typing import Any

from ..input_runtime.diagnostics import (
    InputRuntimeDiagnosticsService,
    RuntimeProcessStatus,
    RuntimeRecoveryNotice,
    RuntimeRecoverySummary,
)
from ..input_runtime.ir9_filesystem import FileSystemRuntimeDiagnosticsReader


_INSTALL_MARKER = "_ir9_runtime_diagnostics_installed"


def install_input_runtime_diagnostics(api: Any) -> None:
    """Attach one diagnostics query service to the production Api instance."""

    if getattr(api, _INSTALL_MARKER, False):
        return
    repositories = getattr(api, "input_runtime_repositories", None)
    root = getattr(repositories, "coordination_root", None)
    locks = getattr(repositories, "coordination_locks", None)
    if repositories is None or root is None or locks is None:
        raise RuntimeError("IR-9 requires coordinated input-runtime repositories")

    reader = FileSystemRuntimeDiagnosticsReader(root=root, locks=locks)

    def process_status() -> RuntimeProcessStatus:
        gate = getattr(api, "input_runtime_readiness_gate", None)
        if gate is None:
            return RuntimeProcessStatus(
                state="failed",
                failure_reason_code="input_runtime_readiness_unavailable",
            )
        state = getattr(gate.state, "value", str(gate.state))
        return RuntimeProcessStatus(
            state=state,
            failure_reason_code=getattr(gate, "failure_reason", None),
        )

    def recovery_summary() -> RuntimeRecoverySummary | None:
        report = getattr(api, "input_runtime_recovery_report", None)
        gate = getattr(api, "input_runtime_readiness_gate", None)
        if report is None and gate is None:
            return None
        fields = report.safe_log_fields() if report is not None else {}
        state = getattr(getattr(gate, "state", None), "value", None)
        result = None
        if state == "ready":
            result = "completed"
        elif state == "failed":
            result = "failed"
        repaired = sum(
            int(fields.get(name, 0))
            for name in (
                "admissions_repaired",
                "committed_unadmitted_admitted",
                "inbox_claims_reconciled",
                "controls_reconciled",
                "handoffs_completed",
                "finalizations_converged",
                "finalizations_aborted",
                "emissions_cancelled",
            )
        )
        ambiguous = int(fields.get("handoffs_ambiguous", 0)) + int(
            fields.get("emissions_unknown", 0)
        )
        return RuntimeRecoverySummary(
            result=result,
            sessions_scanned=int(fields.get("sessions_scanned", 0)),
            repaired_count=repaired,
            ambiguous_count=ambiguous,
            fatal_reason_code=(
                getattr(gate, "failure_reason", None)
                if state == "failed"
                else None
            ),
        )

    def recovery_notice(session_id: str) -> RuntimeRecoveryNotice | None:
        plan = getattr(api, "input_runtime_recovery_plan", None)
        if plan is None:
            return None
        matches = [
            item
            for item in getattr(plan, "sessions", ())
            if getattr(item, "session_id", None) == session_id
        ]
        if not matches:
            return None
        item = matches[-1]
        disposition = getattr(
            getattr(item, "disposition", None),
            "value",
            str(getattr(item, "disposition", "")),
        )
        return RuntimeRecoveryNotice(
            disposition=disposition,
            reason_code=getattr(item, "reason_code", None),
            automatic_replay_enabled=disposition not in {
                "ambiguous",
                "non_resumable",
            },
        )

    api.input_runtime_diagnostics_reader = reader
    api.input_runtime_diagnostics = InputRuntimeDiagnosticsService(
        reader,
        process_status_provider=process_status,
        recovery_summary_provider=recovery_summary,
        recovery_notice_provider=recovery_notice,
    )
    setattr(api, _INSTALL_MARKER, True)
