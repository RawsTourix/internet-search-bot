from datetime import datetime, timedelta, timezone
from enum import Enum

import pytest
from pydantic import ValidationError

from src.input_runtime.models import (
    ActiveCycleSnapshot, AdmissionKind, AdmissionState, AgentEmission,
    CheckpointAction, CheckpointName, CheckpointOutcome, ClaimedInboxRange,
    ControlCommandType, ControlOutcome, ControlState, CycleContextRevision,
    CycleFinalizationRecord, CycleInboxItem, CycleStatus, EmissionState,
    FinalizationState, InboxState, InputAdmissionOutcome, InputAdmissionRecord,
    SessionControlCommand, SessionInputRuntimeState, new_admission_id,
    new_context_revision_id, new_control_id, new_emission_id,
    new_finalization_id, new_inbox_item_id,
)

NOW = datetime(2026, 8, 5, tzinfo=timezone.utc)
LATER = NOW + timedelta(minutes=5)


def admission(**overrides):
    data = dict(session_id=" session ", input_batch_id=" ibat ", session_sequence=1,
                target_cycle_id=" cycle ", cycle_sequence=0, admitted_generation=0,
                admission_kind=AdmissionKind.START_CYCLE, idempotency_key=" key ", admitted_at=NOW)
    data.update(overrides)
    return InputAdmissionRecord(**data)


def inbox(state=InboxState.QUEUED, **overrides):
    data = dict(admission_id=new_admission_id(), session_id="s", cycle_id="c",
                input_batch_id="ibat", cycle_sequence=1, generation=0,
                state=state, enqueued_at=NOW)
    if state in {InboxState.CLAIMED, InboxState.APPLYING}:
        data.update(claim_token="claim", claimed_at=NOW, claim_expires_at=LATER)
    if state == InboxState.APPLIED: data["applied_at"] = LATER
    if state == InboxState.CANCELLED: data["cancelled_at"] = LATER
    if state == InboxState.FAILED_TERMINAL: data["last_error_code"] = "broken"
    data.update(overrides)
    return CycleInboxItem(**data)


def test_stable_ids_and_enums_are_public_contracts():
    for value, prefix in [(new_admission_id(), "adm_"), (new_inbox_item_id(), "inbx_"),
                          (new_control_id(), "ctl_"), (new_context_revision_id(), "ctxrev_"),
                          (new_emission_id(), "emit_"), (new_finalization_id(), "fin_")]:
        assert value.startswith(prefix) and len(value.removeprefix(prefix)) == 32 and value == value.lower()
    for enum_type in (CycleStatus, CheckpointName, AdmissionState, InboxState, ControlState, EmissionState, FinalizationState):
        assert issubclass(enum_type, str) and issubclass(enum_type, Enum)


def test_session_defaults_and_terminal_watermarks():
    state = SessionInputRuntimeState(session_id="s", generation=0, created_at=NOW, updated_at=NOW)
    assert state.accepted_through_session_sequence == 0
    assert state.active_cycle_accepted_through_sequence == 0
    assert state.active_cycle_applied_through_sequence == 0
    assert state.revision == 1
    with pytest.raises(ValidationError):
        SessionInputRuntimeState(session_id="s", generation=0, active_cycle_id="c", cycle_status=CycleStatus.DONE,
            active_cycle_accepted_through_sequence=2, active_cycle_applied_through_sequence=1,
            created_at=NOW, updated_at=NOW)


@pytest.mark.parametrize("value", ["", "   "])
def test_required_identity_strings_are_normalized_and_nonempty(value):
    with pytest.raises(ValidationError): admission(session_id=value)
    record = admission()
    assert (record.session_id, record.target_cycle_id, record.idempotency_key) == ("session", "cycle", "key")


def test_admission_sequence_and_state_contracts():
    assert admission().session_sequence == 1
    with pytest.raises(ValidationError): admission(session_sequence=0)
    with pytest.raises(ValidationError): admission(admission_kind=AdmissionKind.CONTINUE_RUNNING, cycle_sequence=0)
    with pytest.raises(ValidationError): admission(admission_kind=AdmissionKind.START_CYCLE, cycle_sequence=1)
    assert admission(state=AdmissionState.APPLIED, applied_at=LATER).state == AdmissionState.APPLIED
    with pytest.raises(ValidationError): admission(state=AdmissionState.APPLIED)


def test_admission_outcomes_are_stateful():
    record = admission()
    assert InputAdmissionOutcome(outcome="start_cycle", admission=record).admission == record
    assert InputAdmissionOutcome(outcome="capacity_blocked", retryable=True, reason_code="capacity").admission is None
    with pytest.raises(ValidationError): InputAdmissionOutcome(outcome="capacity_blocked")
    with pytest.raises(ValidationError): InputAdmissionOutcome(outcome="bad", admission=record)


@pytest.mark.parametrize("state", list(InboxState))
def test_all_inbox_states_round_trip(state):
    item = inbox(state)
    assert CycleInboxItem.model_validate_json(item.model_dump_json()) == item


def test_claim_range_is_contiguous_and_fenced():
    first, second = inbox(InboxState.CLAIMED), inbox(InboxState.CLAIMED, cycle_sequence=2)
    claim = ClaimedInboxRange(cycle_id="c", generation=0, claim_token="claim", first_cycle_sequence=1,
        last_cycle_sequence=2, items=(first, second), claim_expires_at=LATER)
    assert len(claim.items) == 2
    with pytest.raises(ValidationError): ClaimedInboxRange(cycle_id="c", generation=0, claim_token="claim",
        first_cycle_sequence=1, last_cycle_sequence=3, items=(first, second), claim_expires_at=LATER)


@pytest.mark.parametrize("state", list(ControlState))
def test_all_control_states(state):
    data = dict(session_id="s", target_cycle_id="c", generation=0, sequence_number=1,
                command=ControlCommandType.PAUSE, state=state, idempotency_key="k",
                source_client_type="telegram", created_at=NOW)
    if state in {ControlState.ACKNOWLEDGED, ControlState.APPLIED}: data["acknowledged_at"] = NOW
    if state == ControlState.APPLIED: data["applied_at"] = LATER
    if state == ControlState.REJECTED: data["rejection_code"] = "invalid"
    command = SessionControlCommand(**data)
    assert SessionControlCommand.model_validate_json(command.model_dump_json()) == command


def test_control_outcome_and_reset_target_policy():
    reset = SessionControlCommand(session_id="s", generation=1, sequence_number=1,
        command=ControlCommandType.RESET, idempotency_key="k", source_client_type="web", created_at=NOW)
    assert reset.target_cycle_id is None
    assert ControlOutcome(outcome=ControlState.QUEUED, command=reset).command == reset
    with pytest.raises(ValidationError): ControlOutcome(outcome=ControlState.APPLIED, command=reset)


def snapshot(**overrides):
    data = dict(cycle_id="c", session_id="s", generation=0, status=CycleStatus.RUNNING,
        original_input_batch_id="ibat", original_user_request="request",
        active_context_revision_id=new_context_revision_id(), snapshot_revision=1,
        safe_checkpoint=CheckpointName.BEFORE_LLM, created_at=NOW, updated_at=NOW)
    data.update(overrides)
    return ActiveCycleSnapshot(**data)


def test_snapshot_enums_revision_and_context_id():
    record = snapshot()
    assert record.snapshot_revision == 1 and record.safe_checkpoint == CheckpointName.BEFORE_LLM
    with pytest.raises(ValidationError): snapshot(snapshot_revision=0)
    with pytest.raises(ValidationError): snapshot(active_context_revision_id="ctxrev_bad")
    with pytest.raises(ValidationError): snapshot(status="invented")


@pytest.mark.parametrize("status,field", [(CycleStatus.WAITING_USER, "waiting_question"),
    (CycleStatus.PAUSED_BY_USER, "pause_reason"), (CycleStatus.INTERRUPTED, "interruption_reason")])
def test_snapshot_state_metadata(status, field):
    with pytest.raises(ValidationError): snapshot(status=status)
    assert snapshot(status=status, **{field: "reason"}).status == status


def test_context_revision_parent_ids_and_linear_order():
    initial = CycleContextRevision(cycle_id="c", session_id="s", revision_number=1,
        reason="initial_input", created_at=NOW)
    second = CycleContextRevision(cycle_id="c", session_id="s", revision_number=2,
        parent_revision_ids=[initial.context_revision_id], reason="input_applied", created_at=NOW)
    assert second.parent_revision_ids == [initial.context_revision_id]
    with pytest.raises(ValidationError): CycleContextRevision(cycle_id="c", session_id="s", revision_number=2,
        parent_revision_ids=["ctxrev_bad"], reason="input_applied", created_at=NOW)


@pytest.mark.parametrize("state", list(EmissionState))
def test_all_emission_states(state):
    data = dict(session_id="s", cycle_id="c", context_revision_id=new_context_revision_id(),
        kind="intermediate", text="hello", response_route={"client": "web"}, state=state,
        idempotency_key="k", created_at=NOW)
    if state == EmissionState.DELIVERED: data["delivered_at"] = LATER
    if state in {EmissionState.FAILED, EmissionState.UNKNOWN}: data["error_code"] = "delivery"
    emission = AgentEmission(**data)
    assert AgentEmission.model_validate_json(emission.model_dump_json()) == emission


@pytest.mark.parametrize("state", list(FinalizationState))
def test_all_finalization_states(state):
    data = dict(session_id="s", cycle_id="c", generation=0,
        context_revision_id=new_context_revision_id(), expected_accepted_sequence=1,
        expected_applied_sequence=1, expected_control_sequence=0, state=state,
        created_at=NOW, updated_at=NOW)
    if state in {FinalizationState.RESULT_PERSISTED, FinalizationState.OUTPUT_READY, FinalizationState.TERMINAL_COMMITTED}:
        data["result_ref"] = "result"
    if state == FinalizationState.OUTPUT_READY: data["output_batch_id"] = "output"
    if state in {FinalizationState.FAILED_RECOVERABLE, FinalizationState.FAILED_TERMINAL}:
        data["failure_code"] = "failure"
    record = CycleFinalizationRecord(**data)
    assert CycleFinalizationRecord.model_validate_json(record.model_dump_json()) == record


def test_prepared_requires_equal_watermarks_and_terminal_output_is_optional():
    with pytest.raises(ValidationError): CycleFinalizationRecord(session_id="s", cycle_id="c", generation=0,
        context_revision_id=new_context_revision_id(), expected_accepted_sequence=2, expected_applied_sequence=1,
        expected_control_sequence=0, state=FinalizationState.PREPARED, created_at=NOW, updated_at=NOW)
    terminal = CycleFinalizationRecord(session_id="s", cycle_id="c", generation=0,
        context_revision_id=new_context_revision_id(), expected_accepted_sequence=1, expected_applied_sequence=1,
        expected_control_sequence=0, state=FinalizationState.TERMINAL_COMMITTED,
        result_ref="result", created_at=NOW, updated_at=NOW)
    assert terminal.output_batch_id is None


def test_checkpoint_enums_and_revision_validation():
    outcome = CheckpointOutcome(checkpoint=CheckpointName.AFTER_TOOL_BLOCK,
        action=CheckpointAction.INPUT_APPLIED, context_revision_id=new_context_revision_id(),
        applied_input_batch_ids=("ibat",), applied_through_cycle_sequence=1)
    assert CheckpointOutcome.model_validate_json(outcome.model_dump_json()) == outcome
    with pytest.raises(ValidationError): CheckpointOutcome(checkpoint="random", action=CheckpointAction.CONTINUE)
    with pytest.raises(ValidationError): CheckpointOutcome(checkpoint=CheckpointName.BEFORE_LLM,
        action=CheckpointAction.INPUT_APPLIED, context_revision_id="ctxrev_bad", applied_input_batch_ids=("ibat",))


def test_naive_timestamp_and_json_unsafe_values_rejected():
    with pytest.raises(ValidationError): SessionInputRuntimeState(session_id="s", generation=0,
        created_at=datetime(2026, 8, 5), updated_at=NOW)
    with pytest.raises(ValidationError): AgentEmission(session_id="s", cycle_id="c",
        context_revision_id=new_context_revision_id(), kind="intermediate", text="hello",
        response_route={"bad": {1}}, idempotency_key="k", created_at=NOW)
