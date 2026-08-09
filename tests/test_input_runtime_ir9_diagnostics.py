from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.input_runtime.diagnostics import (
    AddendumProjectionState,
    DiagnosticAdmission,
    DiagnosticControl,
    DiagnosticEmission,
    DiagnosticFinalization,
    DiagnosticHandoff,
    DiagnosticInboxItem,
    InitialRequestProjectionState,
    InputRuntimeDiagnosticsService,
    RuntimeDiagnosticsError,
    RuntimeDiagnosticsRead,
    RuntimeProcessStatus,
    RuntimeRecoveryNotice,
)
from src.input_runtime.handoff import RuntimeHandoffState
from src.input_runtime.models import (
    AdmissionKind,
    AdmissionState,
    ControlCommandType,
    ControlState,
    CycleStatus,
    EmissionState,
    FinalizationState,
    InboxState,
    SessionInputRuntimeState,
    new_finalization_id,
)


NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


class Reader:
    def __init__(self, *reads: RuntimeDiagnosticsRead) -> None:
        self.reads = {item.session_id: item for item in reads}
        self.calls: list[str] = []

    async def read_session(self, session_id: str) -> RuntimeDiagnosticsRead:
        self.calls.append(session_id)
        return self.reads.get(session_id, RuntimeDiagnosticsRead(session_id=session_id))


def state(
    *,
    session_id: str = "session-a",
    cycle_id: str | None = "cycle-a",
    status: CycleStatus = CycleStatus.RUNNING,
    generation: int = 2,
    accepted_session: int = 3,
    accepted: int = 2,
    applied: int = 0,
    pending_control: int = 0,
    applied_control: int = 0,
    finalization_id: str | None = None,
) -> SessionInputRuntimeState:
    if status == CycleStatus.IDLE:
        cycle_id = None
    return SessionInputRuntimeState(
        session_id=session_id,
        generation=generation,
        active_cycle_id=cycle_id,
        cycle_status=status,
        accepted_through_session_sequence=accepted_session,
        active_cycle_accepted_through_sequence=accepted,
        active_cycle_applied_through_sequence=applied,
        pending_control_sequence=pending_control,
        applied_control_sequence=applied_control,
        active_context_revision_id=None,
        finalization_id=finalization_id,
        revision=7,
        created_at=NOW - timedelta(minutes=10),
        updated_at=NOW - timedelta(seconds=5),
    )


def admission(
    sequence: int,
    *,
    session_id: str = "session-a",
    cycle_id: str = "cycle-a",
    generation: int = 2,
    kind: AdmissionKind | None = None,
    state_value: AdmissionState = AdmissionState.ADMITTED,
    admitted_at: datetime = NOW - timedelta(minutes=5),
    reason: str | None = None,
) -> DiagnosticAdmission:
    cycle_sequence = sequence
    resolved_kind = kind or (
        AdmissionKind.START_CYCLE
        if cycle_sequence == 0
        else AdmissionKind.CONTINUE_RUNNING
    )
    return DiagnosticAdmission(
        admission_id=f"admission-{session_id}-{sequence}",
        input_batch_id=f"batch-{session_id}-{sequence}",
        cycle_id=cycle_id,
        cycle_sequence=cycle_sequence,
        session_sequence=sequence + 1,
        generation=generation,
        kind=resolved_kind,
        state=state_value,
        admitted_at=admitted_at,
        applied_at=(NOW - timedelta(minutes=1))
        if state_value == AdmissionState.APPLIED
        else None,
        cancelled_at=(NOW - timedelta(minutes=1))
        if state_value == AdmissionState.CANCELLED
        else None,
        failure_code=reason
        if state_value == AdmissionState.FAILED_TERMINAL
        else None,
        cancellation_reason_code=reason
        if state_value == AdmissionState.CANCELLED
        else None,
    )


def inbox(
    sequence: int,
    *,
    session_id: str = "session-a",
    cycle_id: str = "cycle-a",
    generation: int = 2,
    state_value: InboxState = InboxState.QUEUED,
    enqueued_at: datetime = NOW - timedelta(minutes=4),
    reason: str | None = None,
) -> DiagnosticInboxItem:
    return DiagnosticInboxItem(
        inbox_item_id=f"inbox-{session_id}-{sequence}",
        admission_id=f"admission-{session_id}-{sequence}",
        input_batch_id=f"batch-{session_id}-{sequence}",
        cycle_id=cycle_id,
        cycle_sequence=sequence,
        generation=generation,
        state=state_value,
        attempt_count=1 if state_value != InboxState.QUEUED else 0,
        enqueued_at=enqueued_at,
        claimed_at=(NOW - timedelta(minutes=2))
        if state_value in {InboxState.CLAIMED, InboxState.APPLYING}
        else None,
        applied_at=(NOW - timedelta(minutes=1))
        if state_value == InboxState.APPLIED
        else None,
        cancelled_at=(NOW - timedelta(minutes=1))
        if state_value == InboxState.CANCELLED
        else None,
        last_error_code=reason,
    )


def control(
    sequence: int,
    *,
    command: ControlCommandType = ControlCommandType.PAUSE,
    state_value: ControlState = ControlState.ACKNOWLEDGED,
    generation: int = 2,
    reason: str | None = None,
) -> DiagnosticControl:
    return DiagnosticControl(
        control_id=f"control-{sequence}",
        target_cycle_id="cycle-a",
        generation=generation,
        sequence_number=sequence,
        command=command,
        state=state_value,
        created_at=NOW - timedelta(minutes=3) + timedelta(seconds=sequence),
        acknowledged_at=(NOW - timedelta(minutes=2))
        if state_value in {ControlState.ACKNOWLEDGED, ControlState.APPLIED}
        else None,
        applied_at=(NOW - timedelta(minutes=1))
        if state_value == ControlState.APPLIED
        else None,
        rejection_code=reason if state_value == ControlState.REJECTED else None,
        cancellation_reason_code=reason
        if state_value == ControlState.CANCELLED
        else None,
    )


def emission(
    state_value: EmissionState,
    *,
    emission_id: str | None = None,
    generation: int = 2,
    error_code: str | None = None,
) -> DiagnosticEmission:
    return DiagnosticEmission(
        emission_id=emission_id or f"emission-{state_value.value}",
        cycle_id="cycle-a",
        generation=generation,
        state=state_value,
        importance="normal",
        kind="intermediate",
        created_at=NOW - timedelta(minutes=3),
        delivered_at=NOW - timedelta(minutes=1)
        if state_value == EmissionState.DELIVERED
        else None,
        delivery_claimed_at=NOW - timedelta(minutes=2)
        if state_value == EmissionState.DELIVERING
        else None,
        delivery_attempt_count=1,
        error_code=error_code,
        cancellation_reason_code=(
            "reset_generation_advanced"
            if state_value == EmissionState.CANCELLED
            else None
        ),
    )


def finalization(
    state_value: FinalizationState,
    *,
    finalization_id: str = "finalization-a",
    generation: int = 2,
    failure_code: str | None = None,
) -> DiagnosticFinalization:
    return DiagnosticFinalization(
        finalization_id=finalization_id,
        cycle_id="cycle-a",
        generation=generation,
        state=state_value,
        created_at=NOW - timedelta(minutes=2),
        updated_at=NOW - timedelta(minutes=1),
        failure_code=failure_code,
        cancellation_reason_code=(
            "pause_requested"
            if state_value == FinalizationState.ABORTED_CONTROL
            else None
        ),
        output_batch_id=(
            "output-a"
            if state_value
            in {FinalizationState.OUTPUT_READY, FinalizationState.TERMINAL_COMMITTED}
            else None
        ),
    )


def handoff(
    state_value: RuntimeHandoffState,
    *,
    error_code: str | None = None,
) -> DiagnosticHandoff:
    return DiagnosticHandoff(
        admission_id="admission-session-a-0",
        input_batch_id="batch-session-a-0",
        cycle_id="cycle-a",
        state=state_value,
        handed_off_at=NOW - timedelta(minutes=4),
        completed_at=NOW - timedelta(minutes=1)
        if state_value == RuntimeHandoffState.COMPLETED
        else None,
        ambiguous_at=NOW - timedelta(minutes=1)
        if state_value == RuntimeHandoffState.AMBIGUOUS
        else None,
        error_code=error_code,
    )


def service(read: RuntimeDiagnosticsRead, *, now: datetime = NOW):
    reader = Reader(read)
    return InputRuntimeDiagnosticsService(reader, clock=lambda: now), reader


@pytest.mark.asyncio
async def test_ir9_coherent_running_status_has_required_counts_and_watermarks():
    read = RuntimeDiagnosticsRead(
        session_id="session-a",
        session=state(accepted=4, applied=1, pending_control=2, applied_control=1),
        admissions=(admission(0), admission(1), admission(2), admission(3), admission(4)),
        inbox=(
            inbox(1, state_value=InboxState.APPLIED),
            inbox(2, state_value=InboxState.QUEUED),
            inbox(3, state_value=InboxState.CLAIMED),
            inbox(4, state_value=InboxState.APPLYING),
        ),
        controls=(control(1, state_value=ControlState.APPLIED), control(2)),
        emissions=(emission(EmissionState.READY), emission(EmissionState.UNKNOWN, error_code="delivery_ambiguous")),
    )
    diagnostics, _ = service(read)
    snapshot = await diagnostics.status("session-a")
    assert snapshot.session_status == CycleStatus.RUNNING
    assert snapshot.generation == 2
    assert snapshot.input.accepted_sequence == 4
    assert snapshot.input.applied_sequence == 1
    assert (snapshot.input.queued, snapshot.input.claimed, snapshot.input.applying) == (1, 1, 1)
    assert snapshot.controls.pending_sequence == 2
    assert snapshot.controls.applied_sequence == 1
    assert snapshot.controls.pending_count == 1
    assert snapshot.emissions.ready == 1
    assert snapshot.emissions.unknown == 1


@pytest.mark.asyncio
async def test_ir9_paused_status_is_not_waiting_or_interrupted():
    diagnostics, _ = service(RuntimeDiagnosticsRead(session_id="session-a", session=state(status=CycleStatus.PAUSED_BY_USER)))
    snapshot = await diagnostics.status("session-a")
    assert snapshot.paused is True
    assert snapshot.waiting_for_user is False
    assert snapshot.interrupted is False


@pytest.mark.asyncio
async def test_ir9_waiting_status_is_distinct_from_pause():
    diagnostics, _ = service(RuntimeDiagnosticsRead(session_id="session-a", session=state(status=CycleStatus.WAITING_USER)))
    snapshot = await diagnostics.status("session-a")
    assert snapshot.waiting_for_user is True
    assert snapshot.paused is False


@pytest.mark.asyncio
async def test_ir9_interrupted_status_has_safe_issue_code():
    diagnostics, _ = service(RuntimeDiagnosticsRead(session_id="session-a", session=state(status=CycleStatus.INTERRUPTED)))
    snapshot = await diagnostics.status("session-a")
    assert snapshot.interrupted is True
    assert snapshot.current_issue_code == "runtime_interrupted"


@pytest.mark.asyncio
async def test_ir9_terminal_status_requires_terminal_finalization_and_completed_handoff():
    finalization_id = new_finalization_id()
    read = RuntimeDiagnosticsRead(
        session_id="session-a",
        session=state(
            status=CycleStatus.DONE,
            accepted=2,
            applied=2,
            finalization_id=finalization_id,
        ),
        admissions=(admission(0),),
        handoffs=(handoff(RuntimeHandoffState.COMPLETED),),
        finalizations=(finalization(FinalizationState.TERMINAL_COMMITTED, finalization_id=finalization_id),),
    )
    diagnostics, _ = service(read)
    snapshot = await diagnostics.status("session-a")
    assert snapshot.terminal is True
    assert snapshot.finalization_state == FinalizationState.TERMINAL_COMMITTED
    assert snapshot.handoff_state == RuntimeHandoffState.COMPLETED


@pytest.mark.asyncio
async def test_ir9_terminal_projection_rejects_missing_terminal_marker():
    diagnostics, _ = service(
        RuntimeDiagnosticsRead(
            session_id="session-a",
            session=state(status=CycleStatus.DONE, accepted=2, applied=2),
            admissions=(admission(0),),
            handoffs=(handoff(RuntimeHandoffState.COMPLETED),),
        )
    )
    with pytest.raises(RuntimeDiagnosticsError) as error:
        await diagnostics.status("session-a")
    assert error.value.reason_code == "diagnostics_inconsistent_terminal_authority"


@pytest.mark.asyncio
async def test_ir9_terminal_projection_rejects_noncompleted_handoff():
    finalization_id = new_finalization_id()
    diagnostics, _ = service(
        RuntimeDiagnosticsRead(
            session_id="session-a",
            session=state(status=CycleStatus.DONE, accepted=2, applied=2, finalization_id=finalization_id),
            admissions=(admission(0),),
            handoffs=(handoff(RuntimeHandoffState.HANDED_OFF),),
            finalizations=(finalization(FinalizationState.TERMINAL_COMMITTED, finalization_id=finalization_id),),
        )
    )
    with pytest.raises(RuntimeDiagnosticsError) as error:
        await diagnostics.status("session-a")
    assert error.value.reason_code == "diagnostics_inconsistent_terminal_authority"


@pytest.mark.asyncio
async def test_ir9_status_query_does_not_mutate_coherent_read():
    read = RuntimeDiagnosticsRead(
        session_id="session-a",
        session=state(),
        admissions=(admission(0), admission(1)),
        inbox=(inbox(1),),
    )
    before = read.model_dump(mode="json")
    diagnostics, _ = service(read)
    await diagnostics.status("session-a")
    assert read.model_dump(mode="json") == before


@pytest.mark.asyncio
async def test_ir9_cross_session_isolation_uses_exact_requested_read():
    a = RuntimeDiagnosticsRead(session_id="session-a", session=state(session_id="session-a"), admissions=(admission(0, session_id="session-a"),))
    b = RuntimeDiagnosticsRead(session_id="session-b", session=state(session_id="session-b", cycle_id="cycle-b", accepted=9, applied=9), admissions=(admission(0, session_id="session-b", cycle_id="cycle-b"),))
    reader = Reader(a, b)
    diagnostics = InputRuntimeDiagnosticsService(reader, clock=lambda: NOW)
    snapshot = await diagnostics.status("session-a")
    payload = snapshot.model_dump_json()
    assert reader.calls == ["session-a"]
    assert "session-b" not in payload
    assert "cycle-b" not in payload
    assert snapshot.input.accepted_sequence == 2


@pytest.mark.asyncio
async def test_ir9_old_generation_records_do_not_enter_current_counts():
    read = RuntimeDiagnosticsRead(
        session_id="session-a",
        session=state(generation=3, accepted=1, applied=0),
        admissions=(admission(0, generation=3), admission(1, generation=3), admission(2, generation=2)),
        inbox=(inbox(1, generation=3), inbox(2, generation=2)),
        emissions=(emission(EmissionState.READY, generation=2), emission(EmissionState.READY, generation=3, emission_id="current")),
    )
    diagnostics, _ = service(read)
    snapshot = await diagnostics.status("session-a")
    assert snapshot.input.queued == 1
    assert snapshot.emissions.ready == 1
    assert [item.generation for item in snapshot.additions] == [3]


@pytest.mark.asyncio
async def test_ir9_oldest_queued_age_uses_injected_clock():
    read = RuntimeDiagnosticsRead(
        session_id="session-a",
        session=state(accepted=2),
        admissions=(admission(0), admission(1), admission(2)),
        inbox=(
            inbox(1, enqueued_at=NOW - timedelta(seconds=25)),
            inbox(2, enqueued_at=NOW - timedelta(seconds=7)),
        ),
    )
    diagnostics, _ = service(read)
    snapshot = await diagnostics.status("session-a")
    assert snapshot.input.oldest_queued_age_seconds == 25


@pytest.mark.asyncio
async def test_ir9_future_queued_timestamp_clamps_age_to_zero():
    read = RuntimeDiagnosticsRead(
        session_id="session-a",
        session=state(accepted=1),
        admissions=(admission(0), admission(1)),
        inbox=(inbox(1, enqueued_at=NOW + timedelta(seconds=10)),),
    )
    diagnostics, _ = service(read)
    assert (await diagnostics.status("session-a")).input.oldest_queued_age_seconds == 0


@pytest.mark.asyncio
async def test_ir9_empty_queue_has_null_oldest_age():
    diagnostics, _ = service(RuntimeDiagnosticsRead(session_id="session-a", session=state(accepted=0, applied=0)))
    assert (await diagnostics.status("session-a")).input.oldest_queued_age_seconds is None


@pytest.mark.asyncio
async def test_ir9_addendum_admitted_running_does_not_claim_applied():
    read = RuntimeDiagnosticsRead(
        session_id="session-a",
        session=state(accepted=1),
        admissions=(admission(0), admission(1, kind=AdmissionKind.CONTINUE_RUNNING)),
        inbox=(inbox(1),),
    )
    diagnostics, _ = service(read)
    item = (await diagnostics.status("session-a")).additions[0]
    assert item.state == AddendumProjectionState.ADMITTED
    assert item.acknowledgement == "queued_running"


@pytest.mark.asyncio
async def test_ir9_addendum_admitted_paused_keeps_paused_acknowledgement():
    read = RuntimeDiagnosticsRead(
        session_id="session-a",
        session=state(status=CycleStatus.PAUSED_BY_USER, accepted=1),
        admissions=(admission(0), admission(1, kind=AdmissionKind.QUEUE_PAUSED)),
        inbox=(inbox(1),),
    )
    diagnostics, _ = service(read)
    item = (await diagnostics.status("session-a")).additions[0]
    assert item.state == AddendumProjectionState.ADMITTED
    assert item.acknowledgement == "queued_paused"


@pytest.mark.asyncio
async def test_ir9_waiting_reply_projection_is_resume_waiting_not_new_cycle():
    read = RuntimeDiagnosticsRead(
        session_id="session-a",
        session=state(status=CycleStatus.WAITING_USER, accepted=1),
        admissions=(admission(0), admission(1, kind=AdmissionKind.RESUME_WAITING)),
        inbox=(inbox(1),),
    )
    diagnostics, _ = service(read)
    item = (await diagnostics.status("session-a")).additions[0]
    assert item.cycle_id == "cycle-a"
    assert item.acknowledgement == "resume_waiting"


@pytest.mark.parametrize("inbox_state", [InboxState.CLAIMED, InboxState.APPLYING])
@pytest.mark.asyncio
async def test_ir9_addendum_applying_projection(inbox_state):
    read = RuntimeDiagnosticsRead(
        session_id="session-a",
        session=state(accepted=1),
        admissions=(admission(0), admission(1)),
        inbox=(inbox(1, state_value=inbox_state),),
    )
    diagnostics, _ = service(read)
    assert (await diagnostics.status("session-a")).additions[0].state == AddendumProjectionState.APPLYING


@pytest.mark.asyncio
async def test_ir9_addendum_applied_projection_uses_durable_state():
    read = RuntimeDiagnosticsRead(
        session_id="session-a",
        session=state(accepted=1, applied=1),
        admissions=(admission(0), admission(1, state_value=AdmissionState.APPLIED)),
        inbox=(inbox(1, state_value=InboxState.APPLIED),),
    )
    diagnostics, _ = service(read)
    item = (await diagnostics.status("session-a")).additions[0]
    assert item.state == AddendumProjectionState.APPLIED
    assert item.applied_at is not None


@pytest.mark.asyncio
async def test_ir9_reset_cancelled_addendum_is_explicitly_cancelled():
    read = RuntimeDiagnosticsRead(
        session_id="session-a",
        session=state(accepted=1),
        admissions=(admission(0), admission(1, state_value=AdmissionState.CANCELLED, reason="reset_generation_advanced")),
        inbox=(inbox(1, state_value=InboxState.CANCELLED, reason="reset_generation_advanced"),),
    )
    diagnostics, _ = service(read)
    item = (await diagnostics.status("session-a")).additions[0]
    assert item.state == AddendumProjectionState.CANCELLED
    assert item.reason_code == "reset_generation_advanced"


@pytest.mark.asyncio
async def test_ir9_failed_addendum_uses_safe_reason_code():
    read = RuntimeDiagnosticsRead(
        session_id="session-a",
        session=state(accepted=1),
        admissions=(admission(0), admission(1, state_value=AdmissionState.FAILED_TERMINAL, reason="committed_batch_unavailable")),
        inbox=(inbox(1, state_value=InboxState.FAILED_TERMINAL, reason="committed_batch_unavailable"),),
    )
    diagnostics, _ = service(read)
    item = (await diagnostics.status("session-a")).additions[0]
    assert item.state == AddendumProjectionState.FAILED
    assert item.reason_code == "committed_batch_unavailable"


@pytest.mark.parametrize(
    ("cycle_status", "projection_state"),
    [
        (CycleStatus.RUNNING, InitialRequestProjectionState.RUNNING),
        (CycleStatus.WAITING_USER, InitialRequestProjectionState.WAITING_USER),
        (CycleStatus.PAUSE_REQUESTED, InitialRequestProjectionState.PAUSE_REQUESTED),
        (CycleStatus.PAUSED_BY_USER, InitialRequestProjectionState.PAUSED_BY_USER),
        (CycleStatus.INTERRUPTED, InitialRequestProjectionState.INTERRUPTED),
    ],
)
@pytest.mark.asyncio
async def test_ir9_initial_request_projection_tracks_durable_cycle_state(cycle_status, projection_state):
    diagnostics, _ = service(
        RuntimeDiagnosticsRead(
            session_id="session-a",
            session=state(status=cycle_status, accepted=0, applied=0),
            admissions=(admission(0),),
        )
    )
    initial = (await diagnostics.status("session-a")).initial_request
    assert initial is not None
    assert initial.committed is True
    assert initial.state == projection_state


@pytest.mark.asyncio
async def test_ir9_stop_accepted_is_distinct_from_pause_completed():
    accepted_read = RuntimeDiagnosticsRead(
        session_id="session-a",
        session=state(status=CycleStatus.PAUSE_REQUESTED, pending_control=1, applied_control=0),
        controls=(control(1, state_value=ControlState.ACKNOWLEDGED),),
    )
    paused_read = accepted_read.model_copy(
        update={
            "session": state(status=CycleStatus.PAUSED_BY_USER, pending_control=1, applied_control=1),
            "controls": (control(1, state_value=ControlState.APPLIED),),
        }
    )
    first, _ = service(accepted_read)
    second, _ = service(paused_read)
    accepted = await first.status("session-a")
    paused = await second.status("session-a")
    assert accepted.session_status == CycleStatus.PAUSE_REQUESTED
    assert accepted.controls.effective_state == ControlState.ACKNOWLEDGED
    assert paused.session_status == CycleStatus.PAUSED_BY_USER
    assert paused.controls.effective_state == ControlState.APPLIED


@pytest.mark.asyncio
async def test_ir9_continue_accepted_same_cycle_projection():
    read = RuntimeDiagnosticsRead(
        session_id="session-a",
        session=state(status=CycleStatus.RUNNING, pending_control=1, applied_control=1),
        controls=(control(1, command=ControlCommandType.CONTINUE, state_value=ControlState.APPLIED),),
    )
    diagnostics, _ = service(read)
    snapshot = await diagnostics.status("session-a")
    assert snapshot.active_cycle_id == "cycle-a"
    assert snapshot.controls.effective_command == ControlCommandType.CONTINUE
    assert snapshot.session_status == CycleStatus.RUNNING


@pytest.mark.asyncio
async def test_ir9_continue_rejection_preserves_waiting_state():
    read = RuntimeDiagnosticsRead(
        session_id="session-a",
        session=state(status=CycleStatus.WAITING_USER, pending_control=1, applied_control=1),
        controls=(control(1, command=ControlCommandType.CONTINUE, state_value=ControlState.REJECTED, reason="still_waiting_for_input"),),
    )
    diagnostics, _ = service(read)
    snapshot = await diagnostics.status("session-a")
    assert snapshot.session_status == CycleStatus.WAITING_USER
    assert snapshot.controls.effective_state == ControlState.REJECTED
    assert snapshot.waiting_for_user is True


@pytest.mark.asyncio
async def test_ir9_ambiguous_handoff_disables_automatic_replay():
    read = RuntimeDiagnosticsRead(
        session_id="session-a",
        session=state(status=CycleStatus.INTERRUPTED),
        admissions=(admission(0),),
        handoffs=(handoff(RuntimeHandoffState.AMBIGUOUS, error_code="initial_runtime_handoff_ambiguous"),),
    )
    diagnostics, _ = service(read)
    snapshot = await diagnostics.status("session-a")
    assert snapshot.handoff_state == RuntimeHandoffState.AMBIGUOUS
    assert snapshot.automatic_replay_enabled is False
    assert snapshot.current_issue_code == "initial_runtime_handoff_ambiguous"


@pytest.mark.parametrize("state_value", list(EmissionState))
@pytest.mark.asyncio
async def test_ir9_emission_lifecycle_counts_each_existing_state(state_value):
    error = "delivery_ambiguous" if state_value in {EmissionState.FAILED, EmissionState.UNKNOWN} else None
    read = RuntimeDiagnosticsRead(
        session_id="session-a",
        session=state(),
        emissions=(emission(state_value, error_code=error),),
    )
    diagnostics, _ = service(read)
    counts = (await diagnostics.status("session-a")).emissions
    assert getattr(counts, state_value.value) == 1


@pytest.mark.parametrize("state_value", list(FinalizationState))
@pytest.mark.asyncio
async def test_ir9_finalization_lifecycle_exposes_only_existing_enum_states(state_value):
    failure = "finalization_failed" if state_value in {FinalizationState.FAILED_RECOVERABLE, FinalizationState.FAILED_TERMINAL} else None
    read = RuntimeDiagnosticsRead(
        session_id="session-a",
        session=state(status=CycleStatus.FINALIZING, finalization_id=new_finalization_id()),
        finalizations=(finalization(state_value, finalization_id="different-finalization", failure_code=failure),),
    )
    # FINALIZING points at another id, so selection by exact authority is absent.
    # Rebuild with no explicit finalization id on a non-finalizing state to test
    # projection of every historical/current lifecycle value without fabrication.
    read = read.model_copy(update={"session": state(status=CycleStatus.RUNNING), "finalizations": read.finalizations})
    diagnostics, _ = service(read)
    assert (await diagnostics.status("session-a")).finalization_state == state_value


@pytest.mark.asyncio
async def test_ir9_unknown_emission_is_not_projected_as_failed():
    read = RuntimeDiagnosticsRead(
        session_id="session-a",
        session=state(),
        emissions=(emission(EmissionState.UNKNOWN, error_code="delivery_ambiguous"),),
    )
    diagnostics, _ = service(read)
    snapshot = await diagnostics.status("session-a")
    assert snapshot.emissions.unknown == 1
    assert snapshot.emissions.failed == 0


@pytest.mark.asyncio
async def test_ir9_projection_models_have_no_raw_message_or_route_fields():
    read = RuntimeDiagnosticsRead(
        session_id="session-a",
        session=state(),
        admissions=(admission(0),),
        emissions=(emission(EmissionState.READY),),
    )
    diagnostics, _ = service(read)
    payload = (await diagnostics.status("session-a")).model_dump(mode="json")
    serialized = str(payload)
    for forbidden in (
        "messages_for_llm",
        "original_user_request",
        "response_route",
        "tool_results",
        "callback_auth",
        "api_key",
        "local_path",
        "traceback",
    ):
        assert forbidden not in serialized


@pytest.mark.asyncio
async def test_ir9_timeline_is_bounded_and_marks_truncation():
    admissions = (admission(0),) + tuple(
        admission(index, admitted_at=NOW - timedelta(seconds=index))
        for index in range(1, 35)
    )
    read = RuntimeDiagnosticsRead(
        session_id="session-a",
        session=state(accepted=34),
        admissions=admissions,
    )
    diagnostics, _ = service(read)
    timeline = await diagnostics.timeline("session-a", limit=20)
    assert len(timeline.entries) == 20
    assert timeline.limit == 20
    assert timeline.truncated is True


@pytest.mark.asyncio
async def test_ir9_timeline_same_timestamp_order_is_deterministic():
    same = NOW - timedelta(minutes=1)
    read = RuntimeDiagnosticsRead(
        session_id="session-a",
        session=state(),
        admissions=(admission(0, admitted_at=same), admission(1, admitted_at=same), admission(2, admitted_at=same)),
        controls=(control(1),),
        emissions=(emission(EmissionState.READY, emission_id="emit-b"), emission(EmissionState.READY, emission_id="emit-a")),
        finalizations=(finalization(FinalizationState.PREPARED),),
    )
    diagnostics, _ = service(read)
    first = await diagnostics.timeline("session-a", limit=50)
    second = await diagnostics.timeline("session-a", limit=50)
    assert first.model_dump_json() == second.model_dump_json()


@pytest.mark.parametrize("limit", [0, 101, -1])
@pytest.mark.asyncio
async def test_ir9_timeline_rejects_unbounded_or_invalid_limits(limit):
    diagnostics, _ = service(RuntimeDiagnosticsRead(session_id="session-a", session=state()))
    with pytest.raises(RuntimeDiagnosticsError) as error:
        await diagnostics.timeline("session-a", limit=limit)
    assert error.value.reason_code == "invalid_timeline_limit"


@pytest.mark.asyncio
async def test_ir9_recovering_status_does_not_read_half_recovered_durable_state():
    reader = Reader(RuntimeDiagnosticsRead(session_id="session-a", session=state()))
    diagnostics = InputRuntimeDiagnosticsService(
        reader,
        clock=lambda: NOW,
        process_status_provider=lambda: RuntimeProcessStatus(state="recovering"),
    )
    snapshot = await diagnostics.status("session-a")
    assert snapshot.process_readiness == "recovering"
    assert snapshot.session_exists is False
    assert reader.calls == []


@pytest.mark.asyncio
async def test_ir9_failed_process_status_exposes_only_safe_reason_code():
    reader = Reader(RuntimeDiagnosticsRead(session_id="session-a", session=state()))
    diagnostics = InputRuntimeDiagnosticsService(
        reader,
        clock=lambda: NOW,
        process_status_provider=lambda: RuntimeProcessStatus(
            state="failed",
            failure_reason_code="recovery_structural_failure",
        ),
    )
    snapshot = await diagnostics.status("session-a")
    assert snapshot.process_readiness == "failed"
    assert snapshot.current_issue_code == "recovery_structural_failure"
    assert reader.calls == []


@pytest.mark.asyncio
async def test_ir9_timeline_rejects_query_while_runtime_not_ready():
    reader = Reader(RuntimeDiagnosticsRead(session_id="session-a", session=state()))
    diagnostics = InputRuntimeDiagnosticsService(
        reader,
        process_status_provider=lambda: RuntimeProcessStatus(state="recovering"),
    )
    with pytest.raises(RuntimeDiagnosticsError) as error:
        await diagnostics.timeline("session-a")
    assert error.value.reason_code == "runtime_not_ready"
    assert reader.calls == []


@pytest.mark.asyncio
async def test_ir9_recovery_ambiguous_notice_is_current_issue_and_replay_fence():
    reader = Reader(RuntimeDiagnosticsRead(session_id="session-a", session=state(status=CycleStatus.INTERRUPTED)))
    diagnostics = InputRuntimeDiagnosticsService(
        reader,
        clock=lambda: NOW,
        recovery_notice_provider=lambda _session: RuntimeRecoveryNotice(
            disposition="ambiguous",
            reason_code="runtime_handoff_ambiguous",
            automatic_replay_enabled=False,
        ),
    )
    snapshot = await diagnostics.status("session-a")
    assert snapshot.recovery_notice is not None
    assert snapshot.recovery_notice.disposition == "ambiguous"
    assert snapshot.current_issue_code == "runtime_handoff_ambiguous"
    assert snapshot.automatic_replay_enabled is False


@pytest.mark.asyncio
async def test_ir9_current_failure_priority_prefers_finalization_over_delivery_ambiguity():
    read = RuntimeDiagnosticsRead(
        session_id="session-a",
        session=state(),
        emissions=(emission(EmissionState.UNKNOWN, error_code="delivery_ambiguous"),),
        finalizations=(finalization(FinalizationState.FAILED_RECOVERABLE, failure_code="finalization_recoverable"),),
    )
    diagnostics, _ = service(read)
    snapshot = await diagnostics.status("session-a")
    assert snapshot.current_issue_code == "finalization_recoverable"
    assert snapshot.last_runtime_issue_code in {"finalization_recoverable", "delivery_ambiguous"}
