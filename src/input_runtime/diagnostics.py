"""Transport-neutral IR-9 runtime diagnostics and client projection DTOs.

The service derives bounded, privacy-safe projections from durable IR-1--IR-8
authority. It never becomes admission/control/finalization/recovery authority.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from .handoff import RuntimeHandoffState
from .models import (
    AdmissionKind,
    AdmissionState,
    ControlCommandType,
    ControlState,
    CycleStatus,
    EmissionState,
    FinalizationState,
    InboxState,
    SessionInputRuntimeState,
)


class RuntimeDiagnosticsError(RuntimeError):
    """Controlled diagnostics error safe to expose as a reason code."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = str(reason_code).strip() or "diagnostics_unavailable"
        super().__init__(self.reason_code)


class _ProjectionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=False)


class AddendumProjectionState(str, Enum):
    ADMITTED = "input_addendum_admitted"
    APPLYING = "input_addendum_applying"
    APPLIED = "input_addendum_applied"
    CANCELLED = "input_addendum_cancelled"
    FAILED = "input_addendum_failed"


class InitialRequestProjectionState(str, Enum):
    ADMITTED = "admitted"
    RUNNING = "running"
    WAITING_USER = "waiting_user"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED_BY_USER = "paused_by_user"
    INTERRUPTED = "interrupted"
    FINALIZING = "finalizing"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"


class DiagnosticAdmission(_ProjectionModel):
    admission_id: str
    input_batch_id: str
    cycle_id: str
    cycle_sequence: int = Field(ge=0)
    session_sequence: int = Field(ge=1)
    generation: int = Field(ge=0)
    kind: AdmissionKind
    state: AdmissionState
    admitted_at: datetime
    applied_at: datetime | None = None
    cancelled_at: datetime | None = None
    failure_code: str | None = None
    cancellation_reason_code: str | None = None


class DiagnosticInboxItem(_ProjectionModel):
    inbox_item_id: str
    admission_id: str
    input_batch_id: str
    cycle_id: str
    cycle_sequence: int = Field(ge=1)
    generation: int = Field(ge=0)
    state: InboxState
    attempt_count: int = Field(ge=0)
    enqueued_at: datetime
    claimed_at: datetime | None = None
    applied_at: datetime | None = None
    cancelled_at: datetime | None = None
    last_error_code: str | None = None


class DiagnosticControl(_ProjectionModel):
    control_id: str
    target_cycle_id: str | None = None
    generation: int = Field(ge=0)
    sequence_number: int = Field(ge=1)
    command: ControlCommandType
    state: ControlState
    created_at: datetime
    acknowledged_at: datetime | None = None
    applied_at: datetime | None = None
    rejection_code: str | None = None
    cancellation_reason_code: str | None = None


class DiagnosticHandoff(_ProjectionModel):
    admission_id: str
    input_batch_id: str
    cycle_id: str
    state: RuntimeHandoffState
    handed_off_at: datetime
    completed_at: datetime | None = None
    ambiguous_at: datetime | None = None
    error_code: str | None = None


class DiagnosticEmission(_ProjectionModel):
    emission_id: str
    cycle_id: str
    generation: int = Field(ge=0)
    state: EmissionState
    importance: str
    kind: str
    created_at: datetime
    delivered_at: datetime | None = None
    delivery_claimed_at: datetime | None = None
    delivery_attempt_count: int = Field(ge=0)
    error_code: str | None = None
    cancellation_reason_code: str | None = None


class DiagnosticFinalization(_ProjectionModel):
    finalization_id: str
    cycle_id: str
    generation: int = Field(ge=0)
    state: FinalizationState
    created_at: datetime
    updated_at: datetime
    failure_code: str | None = None
    cancellation_reason_code: str | None = None
    output_batch_id: str | None = None


class RuntimeDiagnosticsRead(_ProjectionModel):
    """One coherent exact-session read prepared inside storage coordination."""

    session_id: str
    session: SessionInputRuntimeState | None = None
    admissions: tuple[DiagnosticAdmission, ...] = ()
    inbox: tuple[DiagnosticInboxItem, ...] = ()
    controls: tuple[DiagnosticControl, ...] = ()
    handoffs: tuple[DiagnosticHandoff, ...] = ()
    emissions: tuple[DiagnosticEmission, ...] = ()
    finalizations: tuple[DiagnosticFinalization, ...] = ()


@runtime_checkable
class RuntimeDiagnosticsReader(Protocol):
    """Storage-neutral read port suitable for a future SQL read transaction."""

    async def read_session(self, session_id: str) -> RuntimeDiagnosticsRead: ...


class RuntimeProcessStatus(_ProjectionModel):
    state: str = "ready"
    failure_reason_code: str | None = None


class RuntimeRecoverySummary(_ProjectionModel):
    result: str | None = None
    sessions_scanned: int = Field(default=0, ge=0)
    repaired_count: int = Field(default=0, ge=0)
    ambiguous_count: int = Field(default=0, ge=0)
    fatal_reason_code: str | None = None


class RuntimeRecoveryNotice(_ProjectionModel):
    disposition: str
    reason_code: str | None = None
    automatic_replay_enabled: bool = True


class RuntimeInputStatus(_ProjectionModel):
    accepted_sequence: int = Field(ge=0)
    applied_sequence: int = Field(ge=0)
    queued: int = Field(default=0, ge=0)
    claimed: int = Field(default=0, ge=0)
    applying: int = Field(default=0, ge=0)
    applied: int = Field(default=0, ge=0)
    cancelled: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    oldest_queued_age_seconds: float | None = Field(default=None, ge=0)


class RuntimeControlStatus(_ProjectionModel):
    pending_sequence: int = Field(ge=0)
    applied_sequence: int = Field(ge=0)
    pending_count: int = Field(default=0, ge=0)
    terminal_count: int = Field(default=0, ge=0)
    effective_command: ControlCommandType | None = None
    effective_state: ControlState | None = None


class RuntimeEmissionCounts(_ProjectionModel):
    ready: int = Field(default=0, ge=0)
    delivering: int = Field(default=0, ge=0)
    delivered: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    unknown: int = Field(default=0, ge=0)
    cancelled: int = Field(default=0, ge=0)


class RuntimeStatusCounts(_ProjectionModel):
    queued_additions: int = Field(default=0, ge=0)
    claimed_additions: int = Field(default=0, ge=0)
    applying_additions: int = Field(default=0, ge=0)
    applied_additions: int = Field(default=0, ge=0)
    pending_controls: int = Field(default=0, ge=0)
    terminal_controls: int = Field(default=0, ge=0)


class RuntimeInitialRequestProjection(_ProjectionModel):
    input_batch_id: str
    admission_id: str
    cycle_id: str
    generation: int = Field(ge=0)
    committed: bool = True
    state: InitialRequestProjectionState
    admitted_at: datetime


class RuntimeAddendumProjection(_ProjectionModel):
    input_batch_id: str
    admission_id: str
    cycle_id: str
    cycle_sequence: int = Field(ge=1)
    generation: int = Field(ge=0)
    state: AddendumProjectionState
    acknowledgement: str
    reason_code: str | None = None
    admitted_at: datetime
    applying_at: datetime | None = None
    applied_at: datetime | None = None
    cancelled_at: datetime | None = None


class RuntimeStatusSnapshot(_ProjectionModel):
    schema_version: str = "ir9.v1"
    process_readiness: str
    session_exists: bool
    session_status: CycleStatus | None = None
    generation: int = Field(default=0, ge=0)
    active_cycle_id: str | None = None
    active_context_revision_id: str | None = None
    accepted_session_sequence: int = Field(default=0, ge=0)
    input: RuntimeInputStatus
    controls: RuntimeControlStatus
    counts: RuntimeStatusCounts
    handoff_state: RuntimeHandoffState | None = None
    automatic_replay_enabled: bool = True
    emissions: RuntimeEmissionCounts
    finalization_id: str | None = None
    finalization_state: FinalizationState | None = None
    waiting_for_user: bool = False
    paused: bool = False
    interrupted: bool = False
    terminal: bool = False
    current_issue_code: str | None = None
    last_runtime_issue_code: str | None = None
    initial_request: RuntimeInitialRequestProjection | None = None
    additions: tuple[RuntimeAddendumProjection, ...] = ()
    recovery: RuntimeRecoverySummary | None = None
    recovery_notice: RuntimeRecoveryNotice | None = None


class RuntimeTimelineEntry(_ProjectionModel):
    kind: str
    state: str
    generation: int = Field(ge=0)
    timestamp: datetime
    sequence: int | None = Field(default=None, ge=0)
    cycle_id: str | None = None
    input_batch_id: str | None = None
    admission_id: str | None = None
    control_id: str | None = None
    emission_id: str | None = None
    finalization_id: str | None = None
    reason_code: str | None = None


class RuntimeTimeline(_ProjectionModel):
    schema_version: str = "ir9.v1"
    session_id: str
    generation: int = Field(ge=0)
    limit: int = Field(ge=1)
    truncated: bool = False
    entries: tuple[RuntimeTimelineEntry, ...] = ()


Clock = Callable[[], datetime]
ProcessStatusProvider = Callable[[], RuntimeProcessStatus]
RecoverySummaryProvider = Callable[[], RuntimeRecoverySummary | None]
RecoveryNoticeProvider = Callable[[str], RuntimeRecoveryNotice | None]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class InputRuntimeDiagnosticsService:
    """Build privacy-safe client projections from one coherent durable read."""

    DEFAULT_TIMELINE_LIMIT = 20
    MAX_TIMELINE_LIMIT = 100
    _TIMELINE_PRIORITY = {
        "session": 0,
        "input": 1,
        "control": 2,
        "handoff": 3,
        "emission": 4,
        "finalization": 5,
    }

    def __init__(
        self,
        reader: RuntimeDiagnosticsReader,
        *,
        clock: Clock | None = None,
        process_status_provider: ProcessStatusProvider | None = None,
        recovery_summary_provider: RecoverySummaryProvider | None = None,
        recovery_notice_provider: RecoveryNoticeProvider | None = None,
    ) -> None:
        self.reader = reader
        self.clock = clock or _utc_now
        self.process_status_provider = process_status_provider or (
            lambda: RuntimeProcessStatus(state="ready")
        )
        self.recovery_summary_provider = recovery_summary_provider or (lambda: None)
        self.recovery_notice_provider = recovery_notice_provider or (lambda _session: None)

    @staticmethod
    def _normalize_session_id(session_id: str) -> str:
        normalized = str(session_id).strip()
        if not normalized:
            raise RuntimeDiagnosticsError("invalid_session_id")
        return normalized

    @staticmethod
    def _normalize_limit(limit: int) -> int:
        try:
            value = int(limit)
        except (TypeError, ValueError) as error:
            raise RuntimeDiagnosticsError("invalid_timeline_limit") from error
        if value < 1 or value > InputRuntimeDiagnosticsService.MAX_TIMELINE_LIMIT:
            raise RuntimeDiagnosticsError("invalid_timeline_limit")
        return value

    def _process_status(self) -> RuntimeProcessStatus:
        try:
            value = self.process_status_provider()
        except Exception as error:
            raise RuntimeDiagnosticsError("diagnostics_unavailable") from error
        return RuntimeProcessStatus.model_validate(value)

    def _recovery_summary(self) -> RuntimeRecoverySummary | None:
        try:
            value = self.recovery_summary_provider()
        except Exception as error:
            raise RuntimeDiagnosticsError("diagnostics_unavailable") from error
        return None if value is None else RuntimeRecoverySummary.model_validate(value)

    def _recovery_notice(self, session_id: str) -> RuntimeRecoveryNotice | None:
        try:
            value = self.recovery_notice_provider(session_id)
        except Exception as error:
            raise RuntimeDiagnosticsError("diagnostics_unavailable") from error
        return None if value is None else RuntimeRecoveryNotice.model_validate(value)

    @staticmethod
    def _empty_status(
        *,
        process: RuntimeProcessStatus,
        recovery: RuntimeRecoverySummary | None,
        issue_code: str | None = None,
    ) -> RuntimeStatusSnapshot:
        return RuntimeStatusSnapshot(
            process_readiness=process.state,
            session_exists=False,
            session_status=None,
            generation=0,
            input=RuntimeInputStatus(accepted_sequence=0, applied_sequence=0),
            controls=RuntimeControlStatus(pending_sequence=0, applied_sequence=0),
            counts=RuntimeStatusCounts(),
            emissions=RuntimeEmissionCounts(),
            current_issue_code=issue_code,
            last_runtime_issue_code=issue_code,
            recovery=recovery,
        )

    async def status(self, session_id: str) -> RuntimeStatusSnapshot:
        normalized = self._normalize_session_id(session_id)
        process = self._process_status()
        recovery = self._recovery_summary()
        if process.state != "ready":
            issue = process.failure_reason_code if process.state == "failed" else None
            return self._empty_status(
                process=process,
                recovery=recovery,
                issue_code=issue,
            )
        try:
            read = await self.reader.read_session(normalized)
        except RuntimeDiagnosticsError:
            raise
        except Exception as error:
            raise RuntimeDiagnosticsError("diagnostics_unavailable") from error
        if read.session_id != normalized:
            raise RuntimeDiagnosticsError("diagnostics_session_scope_mismatch")
        return self._build_status(
            read,
            process=process,
            recovery=recovery,
            recovery_notice=self._recovery_notice(normalized),
        )

    async def timeline(
        self,
        session_id: str,
        *,
        limit: int = DEFAULT_TIMELINE_LIMIT,
    ) -> RuntimeTimeline:
        normalized = self._normalize_session_id(session_id)
        bounded_limit = self._normalize_limit(limit)
        if self._process_status().state != "ready":
            raise RuntimeDiagnosticsError("runtime_not_ready")
        try:
            read = await self.reader.read_session(normalized)
        except RuntimeDiagnosticsError:
            raise
        except Exception as error:
            raise RuntimeDiagnosticsError("diagnostics_unavailable") from error
        if read.session_id != normalized:
            raise RuntimeDiagnosticsError("diagnostics_session_scope_mismatch")
        return self._build_timeline(read, limit=bounded_limit)

    def _build_status(
        self,
        read: RuntimeDiagnosticsRead,
        *,
        process: RuntimeProcessStatus,
        recovery: RuntimeRecoverySummary | None,
        recovery_notice: RuntimeRecoveryNotice | None,
    ) -> RuntimeStatusSnapshot:
        state = read.session
        if state is None:
            return self._empty_status(process=process, recovery=recovery).model_copy(
                update={"recovery_notice": recovery_notice}
            )
        generation = state.generation
        cycle_id = state.active_cycle_id
        admissions = tuple(
            item for item in read.admissions
            if item.generation == generation
            and (cycle_id is None or item.cycle_id == cycle_id)
        )
        inbox = tuple(
            item for item in read.inbox
            if item.generation == generation
            and (cycle_id is None or item.cycle_id == cycle_id)
        )
        controls = tuple(item for item in read.controls if item.generation == generation)
        emissions = tuple(
            item for item in read.emissions
            if item.generation == generation
            and (cycle_id is None or item.cycle_id == cycle_id)
        )
        finalizations = tuple(
            item for item in read.finalizations
            if item.generation == generation
            and (cycle_id is None or item.cycle_id == cycle_id)
        )
        handoffs = tuple(
            item for item in read.handoffs
            if cycle_id is None or item.cycle_id == cycle_id
        )
        inbox_counts = Counter(item.state for item in inbox)
        control_pending = sum(
            item.state in {ControlState.QUEUED, ControlState.ACKNOWLEDGED}
            for item in controls
        )
        control_terminal = sum(
            item.state in {ControlState.APPLIED, ControlState.REJECTED, ControlState.CANCELLED}
            for item in controls
        )
        now = self.clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise RuntimeDiagnosticsError("diagnostics_clock_must_be_timezone_aware")
        now = now.astimezone(timezone.utc)
        queued_times = [
            item.enqueued_at.astimezone(timezone.utc)
            for item in inbox
            if item.state == InboxState.QUEUED
        ]
        oldest_age = None if not queued_times else max(
            0.0,
            (now - min(queued_times)).total_seconds(),
        )
        latest_control = max(
            controls,
            key=lambda item: (item.sequence_number, item.control_id),
            default=None,
        )
        emission_counter = Counter(item.state for item in emissions)
        additions = tuple(
            self._project_addendum(admission, inbox)
            for admission in sorted(
                admissions,
                key=lambda item: (item.cycle_sequence, item.admission_id),
            )
            if admission.cycle_sequence > 0
        )
        initial = self._project_initial_request(state, admissions)
        latest_handoff = max(
            handoffs,
            key=lambda item: (
                item.completed_at or item.ambiguous_at or item.handed_off_at,
                item.admission_id,
            ),
            default=None,
        )
        finalization = self._select_finalization(state, finalizations)
        if state.cycle_status == CycleStatus.DONE:
            if finalization is None or finalization.state != FinalizationState.TERMINAL_COMMITTED:
                raise RuntimeDiagnosticsError("diagnostics_inconsistent_terminal_authority")
            if latest_handoff is None or latest_handoff.state != RuntimeHandoffState.COMPLETED:
                raise RuntimeDiagnosticsError("diagnostics_inconsistent_terminal_authority")
        current_issue, last_issue = self._issue_codes(
            state=state,
            inbox=inbox,
            controls=controls,
            emissions=emissions,
            finalizations=finalizations,
            handoffs=handoffs,
            recovery_notice=recovery_notice,
        )
        automatic_replay_enabled = not any(
            item.state == RuntimeHandoffState.AMBIGUOUS for item in handoffs
        )
        if recovery_notice is not None:
            automatic_replay_enabled = (
                automatic_replay_enabled and recovery_notice.automatic_replay_enabled
            )
        return RuntimeStatusSnapshot(
            process_readiness=process.state,
            session_exists=True,
            session_status=state.cycle_status,
            generation=generation,
            active_cycle_id=cycle_id,
            active_context_revision_id=state.active_context_revision_id,
            accepted_session_sequence=state.accepted_through_session_sequence,
            input=RuntimeInputStatus(
                accepted_sequence=state.active_cycle_accepted_through_sequence,
                applied_sequence=state.active_cycle_applied_through_sequence,
                queued=inbox_counts[InboxState.QUEUED],
                claimed=inbox_counts[InboxState.CLAIMED],
                applying=inbox_counts[InboxState.APPLYING],
                applied=inbox_counts[InboxState.APPLIED],
                cancelled=inbox_counts[InboxState.CANCELLED],
                failed=inbox_counts[InboxState.FAILED_TERMINAL],
                oldest_queued_age_seconds=oldest_age,
            ),
            controls=RuntimeControlStatus(
                pending_sequence=state.pending_control_sequence,
                applied_sequence=state.applied_control_sequence,
                pending_count=control_pending,
                terminal_count=control_terminal,
                effective_command=latest_control.command if latest_control else None,
                effective_state=latest_control.state if latest_control else None,
            ),
            counts=RuntimeStatusCounts(
                queued_additions=inbox_counts[InboxState.QUEUED],
                claimed_additions=inbox_counts[InboxState.CLAIMED],
                applying_additions=inbox_counts[InboxState.APPLYING],
                applied_additions=inbox_counts[InboxState.APPLIED],
                pending_controls=control_pending,
                terminal_controls=control_terminal,
            ),
            handoff_state=latest_handoff.state if latest_handoff else None,
            automatic_replay_enabled=automatic_replay_enabled,
            emissions=RuntimeEmissionCounts(
                ready=emission_counter[EmissionState.READY],
                delivering=emission_counter[EmissionState.DELIVERING],
                delivered=emission_counter[EmissionState.DELIVERED],
                failed=emission_counter[EmissionState.FAILED],
                unknown=emission_counter[EmissionState.UNKNOWN],
                cancelled=emission_counter[EmissionState.CANCELLED],
            ),
            finalization_id=finalization.finalization_id if finalization else None,
            finalization_state=finalization.state if finalization else None,
            waiting_for_user=state.cycle_status == CycleStatus.WAITING_USER,
            paused=state.cycle_status == CycleStatus.PAUSED_BY_USER,
            interrupted=state.cycle_status == CycleStatus.INTERRUPTED,
            terminal=state.cycle_status in {
                CycleStatus.DONE,
                CycleStatus.ERROR,
                CycleStatus.CANCELLED,
            },
            current_issue_code=current_issue,
            last_runtime_issue_code=last_issue,
            initial_request=initial,
            additions=additions,
            recovery=recovery,
            recovery_notice=recovery_notice,
        )

    @staticmethod
    def _select_finalization(
        state: SessionInputRuntimeState,
        finalizations: tuple[DiagnosticFinalization, ...],
    ) -> DiagnosticFinalization | None:
        if state.finalization_id is not None:
            return next(
                (item for item in finalizations if item.finalization_id == state.finalization_id),
                None,
            )
        return max(
            finalizations,
            key=lambda item: (item.updated_at, item.finalization_id),
            default=None,
        )

    @staticmethod
    def _project_initial_request(
        state: SessionInputRuntimeState,
        admissions: tuple[DiagnosticAdmission, ...],
    ) -> RuntimeInitialRequestProjection | None:
        candidates = [
            item for item in admissions
            if item.kind == AdmissionKind.START_CYCLE and item.cycle_sequence == 0
        ]
        if not candidates:
            return None
        admission = max(
            candidates,
            key=lambda item: (item.session_sequence, item.admission_id),
        )
        if admission.state == AdmissionState.CANCELLED:
            projected = InitialRequestProjectionState.CANCELLED
        elif admission.state == AdmissionState.FAILED_TERMINAL:
            projected = InitialRequestProjectionState.ERROR
        else:
            projected = {
                CycleStatus.IDLE: InitialRequestProjectionState.ADMITTED,
                CycleStatus.RUNNING: InitialRequestProjectionState.RUNNING,
                CycleStatus.WAITING_USER: InitialRequestProjectionState.WAITING_USER,
                CycleStatus.PAUSE_REQUESTED: InitialRequestProjectionState.PAUSE_REQUESTED,
                CycleStatus.PAUSED_BY_USER: InitialRequestProjectionState.PAUSED_BY_USER,
                CycleStatus.INTERRUPTED: InitialRequestProjectionState.INTERRUPTED,
                CycleStatus.FINALIZING: InitialRequestProjectionState.FINALIZING,
                CycleStatus.DONE: InitialRequestProjectionState.DONE,
                CycleStatus.ERROR: InitialRequestProjectionState.ERROR,
                CycleStatus.CANCELLED: InitialRequestProjectionState.CANCELLED,
            }[state.cycle_status]
        return RuntimeInitialRequestProjection(
            input_batch_id=admission.input_batch_id,
            admission_id=admission.admission_id,
            cycle_id=admission.cycle_id,
            generation=admission.generation,
            state=projected,
            admitted_at=admission.admitted_at,
        )

    @staticmethod
    def _project_addendum(
        admission: DiagnosticAdmission,
        inbox: tuple[DiagnosticInboxItem, ...],
    ) -> RuntimeAddendumProjection:
        item = next(
            (candidate for candidate in inbox if candidate.admission_id == admission.admission_id),
            None,
        )
        reason_code = None
        applying_at = None
        applied_at = admission.applied_at
        cancelled_at = admission.cancelled_at
        if item is not None:
            applying_at = item.claimed_at
            applied_at = item.applied_at or applied_at
            cancelled_at = item.cancelled_at or cancelled_at
            reason_code = item.last_error_code
        if (
            admission.state == AdmissionState.FAILED_TERMINAL
            or (item is not None and item.state == InboxState.FAILED_TERMINAL)
        ):
            state = AddendumProjectionState.FAILED
            reason_code = reason_code or admission.failure_code
        elif (
            admission.state == AdmissionState.CANCELLED
            or (item is not None and item.state == InboxState.CANCELLED)
        ):
            state = AddendumProjectionState.CANCELLED
            reason_code = (
                reason_code
                or admission.cancellation_reason_code
                or "input_addendum_cancelled"
            )
        elif (
            admission.state == AdmissionState.APPLIED
            or (item is not None and item.state == InboxState.APPLIED)
        ):
            state = AddendumProjectionState.APPLIED
        elif item is not None and item.state in {InboxState.CLAIMED, InboxState.APPLYING}:
            state = AddendumProjectionState.APPLYING
        else:
            state = AddendumProjectionState.ADMITTED
        acknowledgement = {
            AdmissionKind.CONTINUE_RUNNING: "queued_running",
            AdmissionKind.QUEUE_PAUSED: "queued_paused",
            AdmissionKind.RESUME_WAITING: "resume_waiting",
            AdmissionKind.RESUME_INTERRUPTED: "resume_interrupted",
        }.get(admission.kind, "admitted")
        return RuntimeAddendumProjection(
            input_batch_id=admission.input_batch_id,
            admission_id=admission.admission_id,
            cycle_id=admission.cycle_id,
            cycle_sequence=admission.cycle_sequence,
            generation=admission.generation,
            state=state,
            acknowledgement=acknowledgement,
            reason_code=reason_code,
            admitted_at=admission.admitted_at,
            applying_at=applying_at,
            applied_at=applied_at,
            cancelled_at=cancelled_at,
        )

    @staticmethod
    def _issue_codes(
        *,
        state: SessionInputRuntimeState,
        inbox: tuple[DiagnosticInboxItem, ...],
        controls: tuple[DiagnosticControl, ...],
        emissions: tuple[DiagnosticEmission, ...],
        finalizations: tuple[DiagnosticFinalization, ...],
        handoffs: tuple[DiagnosticHandoff, ...],
        recovery_notice: RuntimeRecoveryNotice | None,
    ) -> tuple[str | None, str | None]:
        current_candidates: list[tuple[int, datetime, str, str]] = []
        historical: list[tuple[datetime, int, str, str]] = []

        def add(
            *,
            priority: int,
            when: datetime,
            identity: str,
            code: str | None,
            current: bool,
        ) -> None:
            if not code:
                return
            historical.append((when, priority, identity, code))
            if current:
                current_candidates.append((priority, when, identity, code))

        for item in finalizations:
            if item.state in {
                FinalizationState.FAILED_RECOVERABLE,
                FinalizationState.FAILED_TERMINAL,
            }:
                add(
                    priority=0,
                    when=item.updated_at,
                    identity=item.finalization_id,
                    code=item.failure_code,
                    current=True,
                )
        for item in handoffs:
            if item.state == RuntimeHandoffState.AMBIGUOUS:
                add(
                    priority=1,
                    when=item.ambiguous_at or item.handed_off_at,
                    identity=item.admission_id,
                    code=item.error_code or "runtime_handoff_ambiguous",
                    current=True,
                )
        if state.cycle_status == CycleStatus.INTERRUPTED:
            add(
                priority=2,
                when=state.updated_at,
                identity=state.active_cycle_id or state.session_id,
                code="runtime_interrupted",
                current=True,
            )
        for item in emissions:
            if item.state in {EmissionState.FAILED, EmissionState.UNKNOWN}:
                add(
                    priority=3,
                    when=item.delivered_at or item.delivery_claimed_at or item.created_at,
                    identity=item.emission_id,
                    code=item.error_code,
                    current=item.state == EmissionState.UNKNOWN,
                )
            elif item.state == EmissionState.CANCELLED:
                add(
                    priority=6,
                    when=item.created_at,
                    identity=item.emission_id,
                    code=item.cancellation_reason_code,
                    current=False,
                )
        for item in inbox:
            if item.state in {InboxState.FAILED_TERMINAL, InboxState.CANCELLED}:
                add(
                    priority=4,
                    when=item.cancelled_at or item.enqueued_at,
                    identity=item.inbox_item_id,
                    code=item.last_error_code,
                    current=item.state == InboxState.FAILED_TERMINAL,
                )
        for item in controls:
            if item.state in {ControlState.REJECTED, ControlState.CANCELLED}:
                add(
                    priority=5,
                    when=item.created_at,
                    identity=item.control_id,
                    code=item.rejection_code or item.cancellation_reason_code,
                    current=False,
                )
        if recovery_notice is not None and recovery_notice.reason_code:
            add(
                priority=1 if recovery_notice.disposition == "ambiguous" else 6,
                when=state.updated_at,
                identity=state.active_cycle_id or state.session_id,
                code=recovery_notice.reason_code,
                current=recovery_notice.disposition in {
                    "ambiguous",
                    "interrupted",
                    "non_resumable",
                },
            )
        current_issue = (
            min(
                current_candidates,
                key=lambda item: (item[0], -item[1].timestamp(), item[2]),
            )[3]
            if current_candidates else None
        )
        last_issue = (
            max(
                historical,
                key=lambda item: (item[0].timestamp(), -item[1], item[2]),
            )[3]
            if historical else None
        )
        return current_issue, last_issue

    def _build_timeline(
        self,
        read: RuntimeDiagnosticsRead,
        *,
        limit: int,
    ) -> RuntimeTimeline:
        state = read.session
        if state is None:
            return RuntimeTimeline(
                session_id=read.session_id,
                generation=0,
                limit=limit,
                entries=(),
            )
        generation = state.generation
        cycle_id = state.active_cycle_id
        entries: list[RuntimeTimelineEntry] = [
            RuntimeTimelineEntry(
                kind="session",
                state=state.cycle_status.value,
                generation=generation,
                timestamp=state.updated_at,
                cycle_id=cycle_id,
            )
        ]
        current_admissions = [
            item for item in read.admissions
            if item.generation == generation
            and (cycle_id is None or item.cycle_id == cycle_id)
        ]
        current_inbox = [
            item for item in read.inbox
            if item.generation == generation
            and (cycle_id is None or item.cycle_id == cycle_id)
        ]
        for admission in current_admissions:
            base_state = (
                "initial_request_admitted"
                if admission.cycle_sequence == 0
                else AddendumProjectionState.ADMITTED.value
            )
            entries.append(
                RuntimeTimelineEntry(
                    kind="input",
                    state=base_state,
                    generation=generation,
                    timestamp=admission.admitted_at,
                    sequence=admission.cycle_sequence,
                    cycle_id=admission.cycle_id,
                    input_batch_id=admission.input_batch_id,
                    admission_id=admission.admission_id,
                )
            )
            if admission.cycle_sequence == 0:
                continue
            projected = self._project_addendum(admission, tuple(current_inbox))
            if projected.applying_at is not None:
                entries.append(
                    RuntimeTimelineEntry(
                        kind="input",
                        state=AddendumProjectionState.APPLYING.value,
                        generation=generation,
                        timestamp=projected.applying_at,
                        sequence=admission.cycle_sequence,
                        cycle_id=admission.cycle_id,
                        input_batch_id=admission.input_batch_id,
                        admission_id=admission.admission_id,
                    )
                )
            if projected.state in {
                AddendumProjectionState.APPLIED,
                AddendumProjectionState.CANCELLED,
                AddendumProjectionState.FAILED,
            }:
                entries.append(
                    RuntimeTimelineEntry(
                        kind="input",
                        state=projected.state.value,
                        generation=generation,
                        timestamp=(
                            projected.applied_at
                            or projected.cancelled_at
                            or admission.admitted_at
                        ),
                        sequence=admission.cycle_sequence,
                        cycle_id=admission.cycle_id,
                        input_batch_id=admission.input_batch_id,
                        admission_id=admission.admission_id,
                        reason_code=projected.reason_code,
                    )
                )
        for control in read.controls:
            if control.generation != generation:
                continue
            entries.append(
                RuntimeTimelineEntry(
                    kind="control",
                    state=f"{control.command.value}_accepted",
                    generation=generation,
                    timestamp=control.created_at,
                    sequence=control.sequence_number,
                    cycle_id=control.target_cycle_id,
                    control_id=control.control_id,
                )
            )
            if control.state != ControlState.QUEUED:
                entries.append(
                    RuntimeTimelineEntry(
                        kind="control",
                        state=control.state.value,
                        generation=generation,
                        timestamp=(
                            control.applied_at
                            or control.acknowledged_at
                            or control.created_at
                        ),
                        sequence=control.sequence_number,
                        cycle_id=control.target_cycle_id,
                        control_id=control.control_id,
                        reason_code=(
                            control.rejection_code or control.cancellation_reason_code
                        ),
                    )
                )
        for handoff in read.handoffs:
            if cycle_id is not None and handoff.cycle_id != cycle_id:
                continue
            entries.append(
                RuntimeTimelineEntry(
                    kind="handoff",
                    state=handoff.state.value,
                    generation=generation,
                    timestamp=(
                        handoff.completed_at
                        or handoff.ambiguous_at
                        or handoff.handed_off_at
                    ),
                    cycle_id=handoff.cycle_id,
                    input_batch_id=handoff.input_batch_id,
                    admission_id=handoff.admission_id,
                    reason_code=handoff.error_code,
                )
            )
        for emission in read.emissions:
            if (
                emission.generation != generation
                or (cycle_id is not None and emission.cycle_id != cycle_id)
            ):
                continue
            entries.append(
                RuntimeTimelineEntry(
                    kind="emission",
                    state=emission.state.value,
                    generation=generation,
                    timestamp=(
                        emission.delivered_at
                        or emission.delivery_claimed_at
                        or emission.created_at
                    ),
                    cycle_id=emission.cycle_id,
                    emission_id=emission.emission_id,
                    reason_code=emission.error_code or emission.cancellation_reason_code,
                )
            )
        for finalization in read.finalizations:
            if (
                finalization.generation != generation
                or (cycle_id is not None and finalization.cycle_id != cycle_id)
            ):
                continue
            entries.append(
                RuntimeTimelineEntry(
                    kind="finalization",
                    state=finalization.state.value,
                    generation=generation,
                    timestamp=finalization.updated_at,
                    cycle_id=finalization.cycle_id,
                    finalization_id=finalization.finalization_id,
                    reason_code=(
                        finalization.failure_code
                        or finalization.cancellation_reason_code
                    ),
                )
            )

        def identity(entry: RuntimeTimelineEntry) -> str:
            return (
                entry.finalization_id
                or entry.emission_id
                or entry.control_id
                or entry.admission_id
                or entry.input_batch_id
                or entry.cycle_id
                or ""
            )

        ordered = sorted(
            entries,
            key=lambda entry: (
                entry.timestamp.astimezone(timezone.utc).timestamp(),
                -self._TIMELINE_PRIORITY.get(entry.kind, 99),
                entry.sequence if entry.sequence is not None else -1,
                identity(entry),
                entry.state,
            ),
            reverse=True,
        )
        return RuntimeTimeline(
            session_id=read.session_id,
            generation=generation,
            limit=limit,
            truncated=len(ordered) > limit,
            entries=tuple(ordered[:limit]),
        )
