from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.input_runtime.models import (
    ActiveCycleSnapshot, AgentEmission, CheckpointOutcome, ClaimedInboxRange,
    ControlOutcome, CycleContextRevision, CycleFinalizationRecord,
    CycleInboxItem, InputAdmissionOutcome, InputAdmissionRecord,
    SessionControlCommand, SessionInputRuntimeState, new_admission_id,
    new_context_revision_id, new_control_id, new_emission_id,
    new_finalization_id, new_inbox_item_id,
)

NOW = datetime(2026, 8, 5, tzinfo=timezone.utc)
LATER = NOW + timedelta(minutes=5)


def admission(**overrides):
    values = dict(session_id="s", input_batch_id="ibat-1", session_sequence=1,
                  target_cycle_id="cycle-1", cycle_sequence=0,
                  admitted_generation=0, admission_kind="start_cycle",
                  idempotency_key="ibat-1", admitted_at=NOW)
    values.update(overrides)
    return InputAdmissionRecord(**values)


def inbox_item(sequence=1, state="queued", **overrides):
    values = dict(admission_id=new_admission_id(), session_id="s",
                  cycle_id="cycle-1", input_batch_id=f"ibat-{sequence + 1}",
                  cycle_sequence=sequence, generation=0, state=state,
                  enqueued_at=NOW)
    if state in {"claimed", "applying"}:
        values.update(claim_token="claim-token", claimed_at=NOW,
                      claim_expires_at=LATER)
    values.update(overrides)
    return CycleInboxItem(**values)


def control(state="queued", **overrides):
    values = dict(session_id="s", target_cycle_id="cycle-1", generation=0,
                  sequence_number=1, command="pause", state=state,
                  idempotency_key="pause-1", source_client_type="telegram",
                  created_at=NOW)
    if state in {"acknowledged", "applied"}:
        values["acknowledged_at"] = NOW
    if state == "applied":
        values["applied_at"] = LATER
    if state == "rejected":
        values["rejection_code"] = "invalid_state"
    values.update(overrides)
    return SessionControlCommand(**values)


def snapshot(status="running", **overrides):
    values = dict(cycle_id="cycle-1", session_id="s", generation=0,
                  status=status, original_input_batch_id="ibat-1",
                  original_user_request="request",
                  applied_input_batch_ids=["ibat-1"],
                  applied_through_cycle_sequence=0,
                  active_context_revision_id=new_context_revision_id(),
                  safe_checkpoint="CP-BEFORE-LLM", created_at=NOW,
                  updated_at=NOW)
    values.update(overrides)
    return ActiveCycleSnapshot(**values)


def finalization(state="prepared", **overrides):
    values = dict(session_id="s", cycle_id="cycle-1", generation=0,
                  context_revision_id=new_context_revision_id(),
                  expected_accepted_sequence=0, expected_applied_sequence=0,
                  expected_control_sequence=0, state=state,
                  created_at=NOW, updated_at=NOW)
    if state in {"result_persisted", "output_ready", "terminal_committed"}:
        values["result_ref"] = "result-1"
    if state in {"output_ready", "terminal_committed"}:
        values["output_batch_id"] = "output-1"
    if state in {"failed_recoverable", "failed_terminal"}:
        values["failure_code"] = "failure"
    values.update(overrides)
    return CycleFinalizationRecord(**values)


def test_stable_ids_have_contract_prefix_and_lower_hex():
    for value, prefix in [(new_admission_id(), "adm_"),
                          (new_inbox_item_id(), "inbx_"),
                          (new_control_id(), "ctl_"),
                          (new_context_revision_id(), "ctxrev_"),
                          (new_emission_id(), "emit_"),
                          (new_finalization_id(), "fin_")]:
        assert value.startswith(prefix)
        assert len(value.removeprefix(prefix)) == 32
        assert value == value.lower()


def test_empty_session_watermarks_and_durable_revision_start_at_contract_values():
    state = SessionInputRuntimeState(session_id="s", generation=0,
                                     created_at=NOW, updated_at=NOW)
    assert state.accepted_through_session_sequence == 0
    assert state.active_cycle_accepted_through_sequence == 0
    assert state.active_cycle_applied_through_sequence == 0
    assert state.revision == 1


@pytest.mark.parametrize("changes", [
    {"active_cycle_applied_through_sequence": 2,
     "active_cycle_accepted_through_sequence": 1},
    {"applied_control_sequence": 2, "pending_control_sequence": 1},
    {"cycle_status": "running"},
    {"cycle_status": "idle", "active_cycle_id": "cycle-1"},
    {"cycle_status": "finalizing", "active_cycle_id": "cycle-1"},
    {"cycle_status": "done", "active_cycle_id": "cycle-1",
     "active_cycle_accepted_through_sequence": 2,
     "active_cycle_applied_through_sequence": 1},
])
def test_session_state_rejects_invalid_watermarks_and_status_combinations(changes):
    values = dict(session_id="s", generation=0, created_at=NOW, updated_at=NOW)
    values.update(changes)
    with pytest.raises(ValidationError):
        SessionInputRuntimeState(**values)


def test_naive_timestamp_and_revision_zero_are_rejected():
    with pytest.raises(ValidationError):
        SessionInputRuntimeState(session_id="s", generation=0, revision=0,
                                 created_at=datetime(2026, 8, 5), updated_at=NOW)


def test_admission_sequences_start_at_one_and_only_initial_cycle_sequence_is_zero():
    assert admission().session_sequence == 1
    assert admission(input_batch_id="ibat-2", session_sequence=2,
                     cycle_sequence=1,
                     admission_kind="continue_running").cycle_sequence == 1
    for changes in ({"session_sequence": 0},
                    {"cycle_sequence": 1, "admission_kind": "start_cycle"},
                    {"cycle_sequence": 0,
                     "admission_kind": "continue_running"}):
        with pytest.raises(ValidationError):
            admission(**changes)


@pytest.mark.parametrize("state", ["admitted", "applied", "cancelled", "failed_terminal"])
def test_admission_states_and_json_round_trip(state):
    kwargs = {"state": state}
    if state == "applied": kwargs["applied_at"] = LATER
    if state == "cancelled": kwargs["cancelled_at"] = LATER
    if state == "failed_terminal": kwargs["failure_code"] = "missing_batch"
    record = admission(**kwargs)
    assert InputAdmissionRecord.model_validate_json(record.model_dump_json()) == record


def test_admission_outcomes_cover_success_duplicate_and_capacity():
    record = admission()
    for outcome in ("start_cycle", "queued_running", "resume_waiting",
                    "queued_paused", "resume_interrupted", "duplicate"):
        parsed = InputAdmissionOutcome(outcome=outcome, admission=record)
        assert InputAdmissionOutcome.model_validate_json(parsed.model_dump_json()) == parsed
    blocked = InputAdmissionOutcome(outcome="capacity_blocked", retryable=True,
                                    reason_code="queue_full")
    assert blocked.admission is None
    with pytest.raises(ValidationError):
        InputAdmissionOutcome(outcome="capacity_blocked", retryable=False)


@pytest.mark.parametrize("state", ["queued", "claimed", "applying", "applied", "cancelled", "failed_terminal"])
def test_inbox_states_and_claim_conventions(state):
    kwargs = {}
    if state == "applied": kwargs["applied_at"] = LATER
    if state == "cancelled": kwargs["cancelled_at"] = LATER
    if state == "failed_terminal": kwargs["last_error_code"] = "permanent"
    item = inbox_item(state=state, **kwargs)
    assert CycleInboxItem.model_validate_json(item.model_dump_json()) == item


def test_claim_range_is_contiguous_and_fenced():
    items = (inbox_item(1, "claimed"), inbox_item(2, "claimed"))
    claim = ClaimedInboxRange(cycle_id="cycle-1", generation=0,
                              claim_token="claim-token",
                              first_cycle_sequence=1, last_cycle_sequence=2,
                              items=items, claim_expires_at=LATER)
    assert claim.items == items
    with pytest.raises(ValidationError):
        ClaimedInboxRange(cycle_id="cycle-1", generation=0,
                          claim_token="claim-token", first_cycle_sequence=1,
                          last_cycle_sequence=3, items=items,
                          claim_expires_at=LATER)


@pytest.mark.parametrize("state", ["queued", "acknowledged", "applied", "rejected", "cancelled"])
def test_control_states_outcomes_and_json_round_trip(state):
    command = control(state)
    outcome_name = state if state != "cancelled" else "duplicate"
    outcome = ControlOutcome(outcome=outcome_name, command=command)
    assert ControlOutcome.model_validate_json(outcome.model_dump_json()) == outcome


def test_control_sequence_starts_at_one_and_pause_continue_require_cycle():
    with pytest.raises(ValidationError): control(sequence_number=0)
    with pytest.raises(ValidationError): control(target_cycle_id=None)
    assert control(command="reset", target_cycle_id=None).target_cycle_id is None


@pytest.mark.parametrize("status,extra", [
    ("running", {}), ("waiting_user", {"waiting_question": "question"}),
    ("paused_by_user", {"pause_reason": "user"}),
    ("interrupted", {"interruption_reason": "restart"}),
])
def test_snapshot_statuses_revision_and_json_round_trip(status, extra):
    record = snapshot(status, **extra)
    assert record.snapshot_revision == 1
    assert ActiveCycleSnapshot.model_validate_json(record.model_dump_json()) == record
    with pytest.raises(ValidationError):
        snapshot(status, snapshot_revision=0, **extra)


def test_snapshot_rejects_invalid_plan_revision_and_json_unsafe_history():
    with pytest.raises(ValidationError): snapshot(active_plan_id="plan", active_plan_revision=0)
    with pytest.raises(ValidationError): snapshot(messages_for_llm=[{"bad": {1, 2}}])


def test_context_revisions_are_one_based_linear_and_round_trip():
    initial = CycleContextRevision(cycle_id="cycle-1", session_id="s",
        revision_number=1, reason="initial_input",
        applied_input_batch_ids=["ibat-1"], applied_through_cycle_sequence=0,
        created_at=NOW)
    next_revision = CycleContextRevision(cycle_id="cycle-1", session_id="s",
        revision_number=2, parent_revision_ids=[initial.context_revision_id],
        reason="input_applied", applied_input_batch_ids=["ibat-2"],
        applied_through_cycle_sequence=1, created_at=NOW)
    assert CycleContextRevision.model_validate_json(next_revision.model_dump_json()) == next_revision
    with pytest.raises(ValidationError):
        CycleContextRevision(cycle_id="cycle-1", session_id="s",
            revision_number=2,
            parent_revision_ids=[initial.context_revision_id, new_context_revision_id()],
            reason="input_applied", created_at=NOW)


@pytest.mark.parametrize("state", ["ready", "delivering", "delivered", "failed", "unknown", "cancelled"])
def test_emission_states_json_safety_and_round_trip(state):
    kwargs = {}
    if state == "delivered": kwargs["delivered_at"] = LATER
    if state in {"failed", "unknown"}: kwargs["error_code"] = "delivery"
    emission = AgentEmission(session_id="s", cycle_id="cycle-1",
        context_revision_id=new_context_revision_id(), kind="intermediate",
        text="hello", response_route={"client": "telegram", "chat": 1},
        state=state, idempotency_key="emit-1", created_at=NOW, **kwargs)
    assert AgentEmission.model_validate_json(emission.model_dump_json()) == emission
    with pytest.raises(ValidationError):
        AgentEmission(session_id="s", cycle_id="cycle-1",
            context_revision_id=new_context_revision_id(), kind="intermediate",
            text="hello", response_route={"bad": {1, 2}},
            idempotency_key="bad", created_at=NOW)


@pytest.mark.parametrize("state", ["prepared", "aborted_new_input", "aborted_control",
    "result_persisted", "output_ready", "terminal_committed",
    "failed_recoverable", "failed_terminal"])
def test_all_finalization_states_round_trip(state):
    record = finalization(state)
    assert CycleFinalizationRecord.model_validate_json(record.model_dump_json()) == record


def test_terminal_finalization_requires_equal_input_watermarks():
    with pytest.raises(ValidationError):
        finalization("terminal_committed", expected_accepted_sequence=2,
                     expected_applied_sequence=1)


def test_checkpoint_outcomes_validate_apply_and_reason_contracts():
    applied = CheckpointOutcome(checkpoint="CP-BEFORE-LLM", action="input_applied",
        context_revision_id=new_context_revision_id(),
        applied_through_cycle_sequence=1, applied_input_batch_ids=("ibat-2",))
    assert CheckpointOutcome.model_validate_json(applied.model_dump_json()) == applied
    with pytest.raises(ValidationError):
        CheckpointOutcome(checkpoint="CP-BEFORE-LLM", action="input_applied")
    with pytest.raises(ValidationError):
        CheckpointOutcome(checkpoint="CP-BEFORE-LLM", action="pause")
