"""Transport-neutral IR-8 startup recovery orchestration.

Recovery is deliberately a startup application service, not a normal hot-path
repository scan.  It reconciles durable authority without invoking LLMs, tools,
transports or client delivery.  Safe runtime execution is returned as a plan for
the composition root to install only after the MCP/tool lifecycle is connected.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Protocol

from .admission import InputAdmissionAction, InputAdmissionOutcome
from .errors import InputRuntimeConflictError, InputRuntimeError
from .factory import InputRuntimeRepositories
from .handoff import RuntimeHandoffRecord, RuntimeHandoffState
from .models import (
    ActiveCycleSnapshot,
    AdmissionState,
    CheckpointName,
    ClaimedInboxRange,
    ControlCommandType,
    ControlState,
    CycleFinalizationRecord,
    CycleStatus,
    FinalizationState,
    InboxState,
    InputAdmissionRecord,
    SessionInputRuntimeState,
)

logger = logging.getLogger("InputRuntime.Recovery")


class InputRuntimeLifecycleState(str, Enum):
    RECOVERING = "recovering"
    READY = "ready"
    FAILED = "failed"
    STOPPING = "stopping"
    STOPPED = "stopped"


class InputRuntimeRecoveryError(InputRuntimeError):
    """Safe typed startup failure or readiness rejection."""

    def __init__(self, reason_code: str, *, fatal: bool = True) -> None:
        self.reason_code = reason_code
        self.fatal = fatal
        super().__init__(reason_code)


class InputRuntimeReadinessGate:
    """Process-local lifecycle fence; durable repositories remain authority."""

    def __init__(self) -> None:
        import asyncio

        self._state = InputRuntimeLifecycleState.STOPPED
        self._failure_reason: str | None = None
        self._ready_event = asyncio.Event()

    @property
    def state(self) -> InputRuntimeLifecycleState:
        return self._state

    @property
    def failure_reason(self) -> str | None:
        return self._failure_reason

    @property
    def is_ready(self) -> bool:
        return self._state == InputRuntimeLifecycleState.READY

    def begin_recovery(self) -> None:
        if self._state not in {
            InputRuntimeLifecycleState.STOPPED,
            InputRuntimeLifecycleState.FAILED,
        }:
            raise InputRuntimeRecoveryError("invalid_recovery_gate_transition")
        self._state = InputRuntimeLifecycleState.RECOVERING
        self._failure_reason = None
        self._ready_event.clear()

    def mark_ready(self) -> None:
        if self._state != InputRuntimeLifecycleState.RECOVERING:
            raise InputRuntimeRecoveryError("recovery_gate_not_recovering")
        self._state = InputRuntimeLifecycleState.READY
        self._ready_event.set()

    def mark_failed(self, reason_code: str) -> None:
        if self._state in {
            InputRuntimeLifecycleState.STOPPING,
            InputRuntimeLifecycleState.STOPPED,
        }:
            return
        self._state = InputRuntimeLifecycleState.FAILED
        self._failure_reason = reason_code
        self._ready_event.clear()

    def begin_stopping(self) -> None:
        if self._state == InputRuntimeLifecycleState.STOPPED:
            return
        self._state = InputRuntimeLifecycleState.STOPPING
        self._ready_event.clear()

    def mark_stopped(self) -> None:
        self._state = InputRuntimeLifecycleState.STOPPED
        self._ready_event.clear()

    def require_ready(self) -> None:
        if self._state != InputRuntimeLifecycleState.READY:
            reason = (
                "input_runtime_recovery_failed"
                if self._state == InputRuntimeLifecycleState.FAILED
                else "input_runtime_not_ready"
            )
            raise InputRuntimeRecoveryError(reason, fatal=False)

    async def wait_ready(self) -> None:
        await self._ready_event.wait()
        self.require_ready()


class CommittedBatchRecoveryReader(Protocol):
    async def get_committed(self, input_batch_id: str) -> Any: ...

    async def list_committed_for_recovery(self) -> tuple[Any, ...]: ...


class FinalOutputRecoveryPort(Protocol):
    async def recover_final_output(
        self,
        *,
        record: CycleFinalizationRecord,
        result_payload: dict[str, Any],
    ) -> str: ...


class GenerationCoordinator(Protocol):
    async def synchronize_generation(self, session_id: str, *, generation: int) -> int: ...


@dataclass(slots=True)
class InputRuntimeRecoveryReport:
    sessions_scanned: int = 0
    admissions_repaired: int = 0
    committed_unadmitted_admitted: int = 0
    inbox_claims_reconciled: int = 0
    controls_reconciled: int = 0
    snapshots_validated: int = 0
    handoffs_completed: int = 0
    handoffs_ambiguous: int = 0
    finalizations_converged: int = 0
    finalizations_aborted: int = 0
    emissions_retained: int = 0
    emissions_unknown: int = 0
    emissions_cancelled: int = 0
    resumable_cycles: int = 0
    paused_cycles: int = 0
    waiting_cycles: int = 0
    fatal_consistency_errors: int = 0

    def safe_log_fields(self) -> dict[str, int]:
        return {
            field_name: int(getattr(self, field_name))
            for field_name in self.__dataclass_fields__
        }


class RecoveryDisposition(str, Enum):
    START_ADMITTED = "start_admitted"
    AUTO_RESUME_SAFE = "auto_resume_safe"
    PAUSED = "paused"
    WAITING = "waiting"
    INTERRUPTED = "interrupted"
    AMBIGUOUS = "ambiguous"
    NON_RESUMABLE = "non_resumable"


@dataclass(frozen=True, slots=True)
class RecoverySessionPlan:
    session_id: str
    cycle_id: str
    generation: int
    disposition: RecoveryDisposition
    snapshot: ActiveCycleSnapshot | None = None
    admission_outcome: InputAdmissionOutcome | None = None
    reason_code: str | None = None

    @property
    def should_auto_schedule(self) -> bool:
        return self.disposition in {
            RecoveryDisposition.START_ADMITTED,
            RecoveryDisposition.AUTO_RESUME_SAFE,
        }

    @property
    def should_rehydrate(self) -> bool:
        return self.snapshot is not None and self.disposition in {
            RecoveryDisposition.AUTO_RESUME_SAFE,
            RecoveryDisposition.PAUSED,
            RecoveryDisposition.WAITING,
            RecoveryDisposition.INTERRUPTED,
        }

    @property
    def blocks_explicit_replay(self) -> bool:
        return self.disposition in {
            RecoveryDisposition.AMBIGUOUS,
            RecoveryDisposition.NON_RESUMABLE,
        }


@dataclass(frozen=True, slots=True)
class InputRuntimeRecoveryPlan:
    sessions: tuple[RecoverySessionPlan, ...]
    report: InputRuntimeRecoveryReport

    def blocked_cycles(self) -> dict[str, str]:
        return {
            item.cycle_id: item.reason_code or item.disposition.value
            for item in self.sessions
            if item.blocks_explicit_replay
        }


@dataclass(slots=True)
class _RecoveryCycleView:
    session_id: str
    cycle_id: str
    input_runtime_generation: int


Clock = Callable[[], datetime]


class InputRuntimeRecoveryCoordinator:
    """Two-phase durable discovery/reconciliation for one fresh process."""

    def __init__(
        self,
        *,
        repositories: InputRuntimeRepositories,
        admission_service: Any,
        committed_batches: CommittedBatchRecoveryReader,
        readiness_gate: InputRuntimeReadinessGate,
        generation_coordinator: GenerationCoordinator | None = None,
        final_output_recovery: FinalOutputRecoveryPort | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.repositories = repositories
        self.admission_service = admission_service
        self.committed_batches = committed_batches
        self.readiness_gate = readiness_gate
        self.generation_coordinator = generation_coordinator
        self.final_output_recovery = final_output_recovery
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise InputRuntimeRecoveryError("recovery_clock_not_timezone_aware")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _fatal(reason_code: str) -> InputRuntimeRecoveryError:
        return InputRuntimeRecoveryError(reason_code, fatal=True)

    async def recover(self) -> InputRuntimeRecoveryPlan:
        report = InputRuntimeRecoveryReport()
        self.readiness_gate.begin_recovery()
        logger.info("input_runtime_recovery_started")
        try:
            plan = await self._recover(report)
        except InputRuntimeRecoveryError as error:
            report.fatal_consistency_errors += int(error.fatal)
            self.readiness_gate.mark_failed(error.reason_code)
            logger.error(
                "input_runtime_recovery_failed reason_code=%s fatal=%s",
                error.reason_code,
                error.fatal,
            )
            raise
        except InputRuntimeConflictError as error:
            report.fatal_consistency_errors += 1
            reason = self._safe_conflict_reason(error)
            self.readiness_gate.mark_failed(reason)
            logger.error(
                "input_runtime_recovery_failed reason_code=%s fatal=true",
                reason,
            )
            raise InputRuntimeRecoveryError(reason) from error
        except Exception as error:
            report.fatal_consistency_errors += 1
            self.readiness_gate.mark_failed("input_runtime_recovery_unexpected")
            logger.exception(
                "input_runtime_recovery_failed reason_code=input_runtime_recovery_unexpected"
            )
            raise InputRuntimeRecoveryError(
                "input_runtime_recovery_unexpected"
            ) from error
        logger.info(
            "input_runtime_recovery_completed %s",
            report.safe_log_fields(),
        )
        # READY is intentionally not opened here.  The production composition
        # must first connect MCP, install rehydrated cycles and own/schedule safe
        # runners.  Only then may it call readiness_gate.mark_ready().
        return plan

    @staticmethod
    def _safe_conflict_reason(error: Exception) -> str:
        text = str(error)
        known = {
            "duplicate durable session admission sequence": "duplicate_admission_sequence",
            "duplicate durable cycle admission sequence": "duplicate_cycle_admission_sequence",
            "gap in durable session admission sequence": "admission_sequence_gap",
            "gap in durable cycle admission sequence": "cycle_admission_sequence_gap",
            "cycle authority points to multiple sessions": "divergent_cycle_authority",
            "finalization/handoff identity conflict": "finalization_handoff_identity_conflict",
        }
        for fragment, code in known.items():
            if fragment in text:
                return code
        return "durable_runtime_consistency_conflict"

    async def _recover(
        self,
        report: InputRuntimeRecoveryReport,
    ) -> InputRuntimeRecoveryPlan:
        now = self._now()
        initial_states = await self.repositories.sessions.list_states()
        report.sessions_scanned = len(initial_states)

        await self._repair_identity_and_frontiers(initial_states)
        finalizations = await self._finalizations_for_recovery()
        await self._validate_finalization_uniqueness(finalizations)

        # Strong terminal authority wins before late committed input.  This is
        # the critical COMPLETED-handoff partial-terminal ordering from IR-7.
        await self._preconverge_completed_terminal(finalizations, now, report)

        start_outcomes = await self._recover_committed_inputs(report)
        states = await self.repositories.sessions.list_states()
        await self._repair_identity_and_frontiers(states)

        snapshots = await self.repositories.snapshots.list_active()
        await self._reconcile_snapshot_first_apply(snapshots, now, report)
        await self._recover_controls(now, report)

        # Control/reset reconciliation may have cancelled/reclassified snapshots.
        snapshots = await self.repositories.snapshots.list_active()
        for snapshot in snapshots:
            await self._validate_snapshot(snapshot)
            report.snapshots_validated += 1

        recovered_emissions = await self.repositories.emissions.recover_expired_delivery_claims(
            now=now
        )
        report.emissions_unknown += len(recovered_emissions)
        pending_emissions = await self.repositories.emissions.list_pending_delivery()
        report.emissions_retained += len(pending_emissions)

        await self._recover_finalizations(now, report)
        await self._classify_unfinished_handoffs(now, report)

        # Handoff/finalization classification can change session/snapshot status.
        states = await self.repositories.sessions.list_states()
        if self.generation_coordinator is not None:
            for state in states:
                await self.generation_coordinator.synchronize_generation(
                    state.session_id,
                    generation=state.generation,
                )

        plans = await self._build_session_plans(start_outcomes, now, report)
        return InputRuntimeRecoveryPlan(sessions=plans, report=report)

    async def _repair_identity_and_frontiers(
        self,
        states: tuple[SessionInputRuntimeState, ...],
    ) -> None:
        admissions = await self.repositories.admissions.list_all_for_recovery()  # type: ignore[attr-defined]
        state_ids = {item.session_id for item in states}
        for admission in admissions:
            if admission.session_id not in state_ids:
                raise self._fatal("admission_without_session_state")
            try:
                await self.committed_batches.get_committed(admission.input_batch_id)
            except Exception as error:
                raise self._fatal("missing_referenced_committed_batch") from error

        for state in states:
            await self.repositories.admissions.recover_session_authority(  # type: ignore[attr-defined]
                state.session_id
            )
            await self.repositories.controls.recover_session_authority(  # type: ignore[attr-defined]
                state.session_id
            )

        # Force cycle-authority index repair/validation through every immutable
        # source discovered by startup.  Exact disagreements are rejected by the
        # filesystem adapter rather than resolved by mtime/majority.
        for admission in admissions:
            rows = await self.repositories.inbox.list_for_cycle(
                admission.target_cycle_id
            )
            for row in rows:
                if row.session_id != admission.session_id:
                    raise self._fatal("divergent_cycle_authority")

    async def _finalizations_for_recovery(
        self,
    ) -> tuple[CycleFinalizationRecord, ...]:
        method = getattr(self.repositories.finalizations, "list_for_recovery", None)
        if callable(method):
            return await method()
        return await self.repositories.finalizations.list_recoverable()

    async def _validate_finalization_uniqueness(
        self,
        finalizations: tuple[CycleFinalizationRecord, ...],
    ) -> None:
        by_cycle: dict[tuple[str, str, int], list[CycleFinalizationRecord]] = defaultdict(list)
        for record in finalizations:
            if record.state in {
                FinalizationState.PREPARED,
                FinalizationState.RESULT_PERSISTED,
                FinalizationState.OUTPUT_READY,
                FinalizationState.FAILED_RECOVERABLE,
                FinalizationState.TERMINAL_COMMITTED,
            }:
                by_cycle[(record.session_id, record.cycle_id, record.generation)].append(record)
        for rows in by_cycle.values():
            active = [
                item
                for item in rows
                if item.state != FinalizationState.TERMINAL_COMMITTED
            ]
            terminal = [
                item for item in rows if item.state == FinalizationState.TERMINAL_COMMITTED
            ]
            if len(active) > 1 or len(terminal) > 1 or (active and terminal):
                raise self._fatal("duplicate_finalization_authority")

    async def _handoff_for_finalization(
        self,
        record: CycleFinalizationRecord,
    ) -> RuntimeHandoffRecord | None:
        method = getattr(
            self.repositories.finalizations,
            "get_runtime_handoff_for_recovery",
            None,
        )
        if not callable(method):
            return None
        return await method(record.finalization_id)

    async def _preconverge_completed_terminal(
        self,
        finalizations: tuple[CycleFinalizationRecord, ...],
        now: datetime,
        report: InputRuntimeRecoveryReport,
    ) -> None:
        for record in finalizations:
            if record.state == FinalizationState.TERMINAL_COMMITTED:
                repair = getattr(
                    self.repositories.finalizations,
                    "repair_terminal_projection_for_recovery",
                    None,
                )
                if callable(repair):
                    await repair(record.finalization_id, repaired_at=now)
                continue
            if record.state != FinalizationState.OUTPUT_READY:
                continue
            marker = await self._handoff_for_finalization(record)
            if marker is None or marker.state != RuntimeHandoffState.COMPLETED:
                continue
            converged = await self.admission_service.finalization_service.terminal_commit(
                record.finalization_id
            )
            if converged.state != FinalizationState.TERMINAL_COMMITTED:
                raise self._fatal("completed_handoff_terminal_convergence_failed")
            report.finalizations_converged += 1
            report.handoffs_completed += 1

    async def _recover_committed_inputs(
        self,
        report: InputRuntimeRecoveryReport,
    ) -> dict[str, InputAdmissionOutcome]:
        batches = await self.committed_batches.list_committed_for_recovery()
        start_outcomes: dict[str, InputAdmissionOutcome] = {}
        per_session_last_sequence: dict[str, int] = {}
        for batch in batches:
            session_id = str(batch.session_id)
            sequence = int(getattr(batch, "sequence_number", 0))
            previous = per_session_last_sequence.get(session_id, 0)
            if sequence <= previous:
                raise self._fatal("committed_batch_order_conflict")
            per_session_last_sequence[session_id] = sequence

            existing = await self.repositories.admissions.get_by_input_batch_id(
                batch.input_batch_id
            )
            outcome = await self.admission_service.reconcile_committed_batch(
                batch.input_batch_id,
                session_id=session_id,
            )
            if outcome.action == InputAdmissionAction.CAPACITY_BLOCKED:
                raise self._fatal("committed_batch_recovery_capacity_blocked")
            if existing is None:
                report.committed_unadmitted_admitted += 1
            else:
                report.admissions_repaired += 1
            if (
                outcome.admission is not None
                and outcome.admission.cycle_sequence == 0
                and outcome.admission.state == AdmissionState.ADMITTED
                and outcome.should_start_runner
            ):
                start_outcomes[outcome.target_cycle_id or ""] = outcome
        return start_outcomes

    @staticmethod
    def _claim_from_group(items: list[Any]) -> ClaimedInboxRange:
        ordered = sorted(items, key=lambda item: item.cycle_sequence)
        expected = list(
            range(ordered[0].cycle_sequence, ordered[-1].cycle_sequence + 1)
        )
        if [item.cycle_sequence for item in ordered] != expected:
            raise InputRuntimeRecoveryError("noncontiguous_durable_claim_range")
        expires = ordered[0].claim_expires_at
        if expires is None or any(item.claim_expires_at != expires for item in ordered):
            raise InputRuntimeRecoveryError("claim_lease_identity_conflict")
        return ClaimedInboxRange(
            cycle_id=ordered[0].cycle_id,
            generation=ordered[0].generation,
            claim_token=ordered[0].claim_token or "",
            first_cycle_sequence=ordered[0].cycle_sequence,
            last_cycle_sequence=ordered[-1].cycle_sequence,
            items=tuple(ordered),
            claimed_bytes=sum(item.payload_size_bytes for item in ordered),
            claim_expires_at=expires,
        )

    async def _reconcile_snapshot_first_apply(
        self,
        snapshots: tuple[ActiveCycleSnapshot, ...],
        now: datetime,
        report: InputRuntimeRecoveryReport,
    ) -> None:
        for snapshot in snapshots:
            await self._validate_snapshot(snapshot, allow_session_lag=True)
            items = await self.repositories.inbox.list_for_cycle(snapshot.cycle_id)
            groups: dict[str, list[Any]] = defaultdict(list)
            for item in items:
                if (
                    item.state in {InboxState.CLAIMED, InboxState.APPLYING}
                    and item.claim_token
                    and item.claim_expires_at is not None
                    and item.claim_expires_at <= now
                ):
                    groups[item.claim_token].append(item)
            for group in groups.values():
                states = {item.state for item in group}
                claim = self._claim_from_group(group)
                if states == {InboxState.CLAIMED}:
                    await self.repositories.inbox.requeue_claim(
                        claim,
                        error_code="startup_claim_expired",
                    )
                    report.inbox_claims_reconciled += 1
                    continue
                if states != {InboxState.APPLYING}:
                    raise self._fatal("mixed_claim_apply_state")
                first = claim.first_cycle_sequence
                last = claim.last_cycle_sequence
                applied = snapshot.applied_through_cycle_sequence
                if first <= applied < last:
                    raise self._fatal("partial_claim_snapshot_authority")
                if last <= applied:
                    applied_ids = set(snapshot.applied_input_batch_ids)
                    if any(item.input_batch_id not in applied_ids for item in claim.items):
                        raise self._fatal("snapshot_applied_batch_identity_gap")
                    await self.repositories.inbox.mark_applied(claim, applied_at=now)
                    for item in claim.items:
                        admission = await self.repositories.admissions.get_by_input_batch_id(
                            item.input_batch_id
                        )
                        if admission is None:
                            raise self._fatal("inbox_admission_missing")
                        if admission.state == AdmissionState.ADMITTED:
                            await self.repositories.admissions.mark_applied(
                                admission.admission_id,
                                applied_at=now,
                            )
                    report.inbox_claims_reconciled += 1
                else:
                    await self.repositories.inbox.requeue_claim(
                        claim,
                        error_code="startup_apply_not_committed",
                    )
                    report.inbox_claims_reconciled += 1

            # Snapshot-first authority also repairs marker writes that crashed
            # after context persistence, without creating a new revision/update.
            admissions = await self.repositories.admissions.list_for_session(
                snapshot.session_id
            )
            cycle_rows = sorted(
                (
                    item
                    for item in admissions
                    if item.target_cycle_id == snapshot.cycle_id
                    and item.admitted_generation == snapshot.generation
                ),
                key=lambda item: item.cycle_sequence,
            )
            by_sequence = {item.cycle_sequence: item for item in cycle_rows}
            expected = list(range(0, snapshot.applied_through_cycle_sequence + 1))
            if any(sequence not in by_sequence for sequence in expected):
                raise self._fatal("snapshot_applied_sequence_gap")
            expected_ids = [by_sequence[sequence].input_batch_id for sequence in expected]
            if snapshot.applied_input_batch_ids != expected_ids:
                raise self._fatal("snapshot_applied_batch_identity_mismatch")
            for sequence in expected:
                admission = by_sequence[sequence]
                if admission.state == AdmissionState.ADMITTED:
                    await self.repositories.admissions.mark_applied(
                        admission.admission_id,
                        applied_at=now,
                    )
            state = await self.repositories.sessions.get(snapshot.session_id)
            if state is None:
                raise self._fatal("snapshot_session_missing")
            await self.admission_service.cycle_input_applier._advance_session_authority(
                state=state,
                context_revision_id=snapshot.active_context_revision_id,
                applied_through=snapshot.applied_through_cycle_sequence,
                now=now,
            )

    async def _recover_controls(
        self,
        now: datetime,
        report: InputRuntimeRecoveryReport,
    ) -> None:
        states = await self.repositories.sessions.list_states()
        for initial in states:
            rows = await self.repositories.controls.list_for_session(initial.session_id)  # type: ignore[attr-defined]
            for row in rows:
                if (
                    row.command == ControlCommandType.RESET
                    and row.state not in {
                        ControlState.APPLIED,
                        ControlState.REJECTED,
                        ControlState.CANCELLED,
                    }
                ):
                    await self.admission_service.control_service._reconcile_reset(row)
                    report.controls_reconciled += 1
            state = await self.repositories.sessions.get(initial.session_id)
            if state is None or state.pending_control_sequence <= state.applied_control_sequence:
                continue
            if state.active_cycle_id is None:
                raise self._fatal("pending_control_without_active_cycle")
            snapshot = await self.repositories.snapshots.get(state.active_cycle_id)
            if snapshot is None:
                raise self._fatal("pending_control_snapshot_missing")
            view = _RecoveryCycleView(
                session_id=state.session_id,
                cycle_id=state.active_cycle_id,
                input_runtime_generation=state.generation,
            )
            outcome = await self.admission_service.control_service.reduce_at_checkpoint(
                checkpoint=CheckpointName.RESUME,
                active_cycle=view,
                through_control_sequence=state.pending_control_sequence,
            )
            if outcome is not None and outcome.reason_code == "reset_generation_transition_pending":
                raise self._fatal("reset_generation_recovery_incomplete")
            report.controls_reconciled += 1

    @staticmethod
    def _validate_message_protocol(messages: list[dict[str, Any]]) -> None:
        pending: set[str] = set()
        for message in messages:
            role = message.get("role")
            if pending and role != "tool":
                raise InputRuntimeRecoveryError("incomplete_tool_result_block")
            if role == "tool":
                tool_call_id = str(message.get("tool_call_id") or "")
                if not tool_call_id or tool_call_id not in pending:
                    raise InputRuntimeRecoveryError("orphan_tool_result")
                pending.remove(tool_call_id)
                continue
            if role == "assistant" and message.get("tool_calls"):
                calls = message.get("tool_calls")
                if not isinstance(calls, list):
                    raise InputRuntimeRecoveryError("invalid_tool_call_block")
                ids = [str(item.get("id") or "") for item in calls if isinstance(item, dict)]
                if not ids or any(not item for item in ids) or len(ids) != len(set(ids)):
                    raise InputRuntimeRecoveryError("invalid_tool_call_identity")
                pending = set(ids)
        if pending:
            raise InputRuntimeRecoveryError("incomplete_tool_result_block")

    async def _validate_snapshot(
        self,
        snapshot: ActiveCycleSnapshot,
        *,
        allow_session_lag: bool = False,
    ) -> None:
        state = await self.repositories.sessions.get(snapshot.session_id)
        if state is None:
            raise self._fatal("snapshot_session_missing")
        if (
            state.generation != snapshot.generation
            or state.active_cycle_id != snapshot.cycle_id
        ):
            raise self._fatal("snapshot_cycle_ownership_mismatch")
        revision = await self.repositories.context_revisions.get(
            snapshot.active_context_revision_id
        )
        if revision is None:
            raise self._fatal("active_context_revision_missing")
        if (
            revision.session_id != snapshot.session_id
            or revision.cycle_id != snapshot.cycle_id
            or revision.applied_through_cycle_sequence
            != snapshot.applied_through_cycle_sequence
        ):
            raise self._fatal("snapshot_context_revision_mismatch")
        if (
            not allow_session_lag
            and state.active_context_revision_id != snapshot.active_context_revision_id
        ):
            raise self._fatal("session_context_revision_mismatch")
        try:
            original = await self.committed_batches.get_committed(
                snapshot.original_input_batch_id
            )
        except Exception as error:
            raise self._fatal("missing_original_committed_batch") from error
        if str(original.session_id) != snapshot.session_id:
            raise self._fatal("original_committed_batch_session_mismatch")
        for batch_id in snapshot.applied_input_batch_ids:
            try:
                batch = await self.committed_batches.get_committed(batch_id)
            except Exception as error:
                raise self._fatal("missing_applied_committed_batch") from error
            if str(batch.session_id) != snapshot.session_id:
                raise self._fatal("applied_committed_batch_session_mismatch")
        self._validate_message_protocol(snapshot.messages_for_llm)
        if snapshot.working_memory_ref is not None:
            # No current v0.4 durable working-memory resolver is composed.  Do
            # not silently replace such state with an empty object on restart.
            raise InputRuntimeRecoveryError(
                "working_memory_reference_requires_explicit_resolver",
                fatal=False,
            )

    async def _recover_finalizations(
        self,
        now: datetime,
        report: InputRuntimeRecoveryReport,
    ) -> None:
        records = await self._finalizations_for_recovery()
        for record in records:
            if record.state == FinalizationState.TERMINAL_COMMITTED:
                repair = getattr(
                    self.repositories.finalizations,
                    "repair_terminal_projection_for_recovery",
                    None,
                )
                if callable(repair):
                    await repair(record.finalization_id, repaired_at=now)
                continue
            if record.state == FinalizationState.FAILED_RECOVERABLE:
                continue
            recheck = getattr(
                self.repositories.finalizations,
                "recheck_recoverable_authority",
                None,
            )
            current = (
                await recheck(record.finalization_id, checked_at=now)
                if callable(recheck)
                else record
            )
            if current.state in {
                FinalizationState.ABORTED_NEW_INPUT,
                FinalizationState.ABORTED_CONTROL,
            }:
                report.finalizations_aborted += 1
                continue
            if current.state == FinalizationState.PREPARED:
                abandon = getattr(
                    self.repositories.finalizations,
                    "abandon_prepared_for_recovery",
                    None,
                )
                if not callable(abandon):
                    raise self._fatal("prepared_finalization_recovery_unavailable")
                await abandon(
                    current.finalization_id,
                    interrupted_at=now,
                    reason_code="startup_prepared_requires_explicit_resume",
                )
                report.finalizations_aborted += 1
                await self._interrupt_if_current(
                    current,
                    reason_code="startup_prepared_requires_explicit_resume",
                    now=now,
                )
                continue
            if current.state == FinalizationState.RESULT_PERSISTED:
                if self.final_output_recovery is None:
                    raise self._fatal("final_output_recovery_not_composed")
                loader = getattr(
                    self.repositories.finalizations,
                    "load_result_payload_for_recovery",
                    None,
                )
                if not callable(loader):
                    raise self._fatal("persisted_result_recovery_unavailable")
                payload = await loader(current.finalization_id)
                output_batch_id = await self.final_output_recovery.recover_final_output(
                    record=current,
                    result_payload=payload,
                )
                current = await self.admission_service.finalization_service.mark_output_ready(
                    current.finalization_id,
                    output_batch_id=output_batch_id,
                )
            if current.state == FinalizationState.OUTPUT_READY:
                current = await self.admission_service.finalization_service.terminal_commit(
                    current.finalization_id
                )
                if current.state == FinalizationState.TERMINAL_COMMITTED:
                    report.finalizations_converged += 1
                    marker = await self._handoff_for_finalization(current)
                    if marker is not None and marker.state == RuntimeHandoffState.COMPLETED:
                        report.handoffs_completed += 1
                elif current.state in {
                    FinalizationState.ABORTED_NEW_INPUT,
                    FinalizationState.ABORTED_CONTROL,
                }:
                    report.finalizations_aborted += 1

    async def _interrupt_if_current(
        self,
        record: CycleFinalizationRecord,
        *,
        reason_code: str,
        now: datetime,
    ) -> None:
        state = await self.repositories.sessions.get(record.session_id)
        snapshot = await self.repositories.snapshots.get(record.cycle_id)
        if (
            state is None
            or snapshot is None
            or state.generation != record.generation
            or state.active_cycle_id != record.cycle_id
        ):
            return
        if snapshot.status in {
            CycleStatus.DONE,
            CycleStatus.ERROR,
            CycleStatus.CANCELLED,
        }:
            return
        await self.repositories.snapshots.mark_recovery_interrupted(  # type: ignore[attr-defined]
            session_id=record.session_id,
            cycle_id=record.cycle_id,
            generation=record.generation,
            reason_code=reason_code,
            interrupted_at=now,
        )

    async def _classify_unfinished_handoffs(
        self,
        now: datetime,
        report: InputRuntimeRecoveryReport,
    ) -> None:
        method = getattr(self.repositories.handoffs, "list_nonterminal_for_recovery", None)
        if not callable(method):
            return
        rows = await method()
        grouped: dict[tuple[str, str], list[RuntimeHandoffRecord]] = defaultdict(list)
        for row in rows:
            grouped[(row.session_id, row.cycle_id)].append(row)
        for group in grouped.values():
            if len(group) > 1:
                raise self._fatal("multiple_nonterminal_runtime_handoffs")
            marker = group[0]
            if marker.state == RuntimeHandoffState.HANDED_OFF:
                marker = await self.repositories.handoffs.mark_ambiguous(
                    marker.admission_id,
                    handoff_token=marker.handoff_token,
                    ambiguous_at=now,
                    error_code="startup_process_lost_after_handoff",
                )
                report.handoffs_ambiguous += 1
            state = await self.repositories.sessions.get(marker.session_id)
            snapshot = await self.repositories.snapshots.get(marker.cycle_id)
            if (
                state is not None
                and snapshot is not None
                and state.generation == snapshot.generation
                and state.active_cycle_id == marker.cycle_id
                and snapshot.status not in {
                    CycleStatus.DONE,
                    CycleStatus.ERROR,
                    CycleStatus.CANCELLED,
                }
            ):
                await self.repositories.snapshots.mark_recovery_interrupted(  # type: ignore[attr-defined]
                    session_id=marker.session_id,
                    cycle_id=marker.cycle_id,
                    generation=snapshot.generation,
                    reason_code="ambiguous_runtime_handoff",
                    interrupted_at=now,
                )

    async def _build_session_plans(
        self,
        start_outcomes: dict[str, InputAdmissionOutcome],
        now: datetime,
        report: InputRuntimeRecoveryReport,
    ) -> tuple[RecoverySessionPlan, ...]:
        plans: list[RecoverySessionPlan] = []
        states = await self.repositories.sessions.list_states()
        nonterminal_handoffs = {
            item.cycle_id: item
            for item in await self.repositories.handoffs.list_nonterminal_for_recovery()  # type: ignore[attr-defined]
        }
        for state in sorted(states, key=lambda item: item.session_id):
            cycle_id = state.active_cycle_id
            if cycle_id is None or state.cycle_status in {
                CycleStatus.IDLE,
                CycleStatus.DONE,
                CycleStatus.ERROR,
                CycleStatus.CANCELLED,
            }:
                continue
            snapshot = await self.repositories.snapshots.get(cycle_id)
            if snapshot is None:
                outcome = start_outcomes.get(cycle_id)
                if (
                    outcome is not None
                    and outcome.admission is not None
                    and outcome.admission.state == AdmissionState.ADMITTED
                    and await self.repositories.handoffs.get(outcome.admission.admission_id) is None
                ):
                    plans.append(
                        RecoverySessionPlan(
                            session_id=state.session_id,
                            cycle_id=cycle_id,
                            generation=state.generation,
                            disposition=RecoveryDisposition.START_ADMITTED,
                            admission_outcome=outcome,
                        )
                    )
                    report.resumable_cycles += 1
                    continue
                raise self._fatal("active_cycle_snapshot_missing")

            try:
                await self._validate_snapshot(snapshot)
            except InputRuntimeRecoveryError as error:
                if error.fatal:
                    raise
                plans.append(
                    RecoverySessionPlan(
                        session_id=state.session_id,
                        cycle_id=cycle_id,
                        generation=state.generation,
                        disposition=RecoveryDisposition.NON_RESUMABLE,
                        snapshot=snapshot,
                        reason_code=error.reason_code,
                    )
                )
                continue

            marker = nonterminal_handoffs.get(cycle_id)
            if marker is not None and marker.state == RuntimeHandoffState.AMBIGUOUS:
                plans.append(
                    RecoverySessionPlan(
                        session_id=state.session_id,
                        cycle_id=cycle_id,
                        generation=state.generation,
                        disposition=RecoveryDisposition.AMBIGUOUS,
                        snapshot=snapshot,
                        reason_code=marker.error_code or "ambiguous_runtime_handoff",
                    )
                )
                continue

            if state.cycle_status == CycleStatus.WAITING_USER:
                if snapshot.status != CycleStatus.WAITING_USER or not snapshot.waiting_question:
                    raise self._fatal("waiting_snapshot_authority_mismatch")
                plans.append(
                    RecoverySessionPlan(
                        state.session_id,
                        cycle_id,
                        state.generation,
                        RecoveryDisposition.WAITING,
                        snapshot,
                    )
                )
                report.waiting_cycles += 1
                report.resumable_cycles += 1
                continue
            if state.cycle_status == CycleStatus.PAUSED_BY_USER:
                if snapshot.status != CycleStatus.PAUSED_BY_USER:
                    raise self._fatal("paused_snapshot_authority_mismatch")
                plans.append(
                    RecoverySessionPlan(
                        state.session_id,
                        cycle_id,
                        state.generation,
                        RecoveryDisposition.PAUSED,
                        snapshot,
                    )
                )
                report.paused_cycles += 1
                report.resumable_cycles += 1
                continue
            if state.cycle_status == CycleStatus.PAUSE_REQUESTED:
                raise self._fatal("pause_requested_not_reconciled")
            if state.cycle_status == CycleStatus.FINALIZING:
                raise self._fatal("finalizing_state_not_reconciled")
            if state.cycle_status == CycleStatus.RUNNING:
                snapshot = await self.repositories.snapshots.mark_recovery_interrupted(  # type: ignore[attr-defined]
                    session_id=state.session_id,
                    cycle_id=cycle_id,
                    generation=state.generation,
                    reason_code="startup_safe_restart",
                    interrupted_at=now,
                )
                plans.append(
                    RecoverySessionPlan(
                        state.session_id,
                        cycle_id,
                        state.generation,
                        RecoveryDisposition.AUTO_RESUME_SAFE,
                        snapshot,
                        reason_code="startup_safe_restart",
                    )
                )
                report.resumable_cycles += 1
                continue
            if state.cycle_status == CycleStatus.INTERRUPTED:
                plans.append(
                    RecoverySessionPlan(
                        state.session_id,
                        cycle_id,
                        state.generation,
                        RecoveryDisposition.INTERRUPTED,
                        snapshot,
                        reason_code=snapshot.interruption_reason,
                    )
                )
                report.resumable_cycles += 1
                continue
            raise self._fatal("unsupported_recovery_cycle_status")
        return tuple(plans)
