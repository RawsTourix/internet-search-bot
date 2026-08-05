from datetime import datetime, timedelta, timezone
import pytest
from pydantic import ValidationError
from src.input_runtime import *

NOW = datetime(2026, 8, 5, tzinfo=timezone.utc)


def test_id_factories_and_validators():
    pairs = [
        (new_input_admission_id, is_input_admission_id, "adm_"),
        (new_cycle_inbox_item_id, is_cycle_inbox_item_id, "inbx_"),
        (new_session_control_id, is_session_control_id, "ctl_"),
        (new_context_revision_id, is_context_revision_id, "ctxrev_"),
        (new_agent_emission_id, is_agent_emission_id, "emit_"),
        (new_finalization_id, is_finalization_id, "fin_"),
    ]
    for factory, validator, prefix in pairs:
        value = factory()
        assert validator(value)
        for invalid in ("", value.upper(), prefix + "a" * 31, prefix + "a" * 33, prefix + "g" * 32, "bad_" + value.split("_", 1)[1]):
            assert not validator(invalid)


def make_state(**changes):
    data = dict(
        session_id="s", generation=0, active_cycle_id=None,
        cycle_status="idle", accepted_through_session_sequence=0,
        active_cycle_accepted_through_sequence=0,
        active_cycle_applied_through_sequence=0,
        pending_control_sequence=0, applied_control_sequence=0,
        active_context_revision_id=None, finalization_id=None,
        revision=1, created_at=NOW, updated_at=NOW,
    )
    data.update(changes)
    return SessionInputRuntimeState(**data)


def test_session_state_and_timestamp_invariants():
    assert make_state().cycle_status is CycleRuntimeStatus.IDLE
    assert make_state(active_cycle_id="c", cycle_status="running").active_cycle_id == "c"
    invalid = [
        {"active_cycle_applied_through_sequence": 1},
        {"applied_control_sequence": 1},
        {"active_cycle_id": "c"},
        {"cycle_status": "running"},
        {"active_cycle_id": "c", "cycle_status": "finalizing"},
        {"active_cycle_id": "c", "cycle_status": "done", "active_cycle_accepted_through_sequence": 2, "active_cycle_applied_through_sequence": 1},
    ]
    for changes in invalid:
        with pytest.raises(ValidationError):
            make_state(**changes)
    with pytest.raises(ValidationError):
        make_state(created_at=datetime.now())
    converted = make_state(created_at=datetime(2026, 8, 5, 3, tzinfo=timezone(timedelta(hours=3))))
    assert converted.created_at.tzinfo == timezone.utc


def make_admission(kind="start_cycle", sequence=0, state="admitted", **changes):
    data = dict(
        admission_id=new_input_admission_id(), session_id="s",
        input_batch_id="b", session_sequence=1, target_cycle_id="c",
        cycle_sequence=sequence, admitted_generation=0,
        admission_kind=kind, state=state, idempotency_key="k",
        admitted_at=NOW,
    )
    data.update(changes)
    return InputAdmissionRecord(**data)


def test_admission_and_serialization_invariants():
    make_admission()
    make_admission("continue_running", 1)
    for args in (("start_cycle", 1), ("continue_running", 0)):
        with pytest.raises(ValidationError):
            make_admission(*args)
    with pytest.raises(ValidationError):
        make_admission(state="applied")
    with pytest.raises(ValidationError):
        make_admission(state="failed_terminal")
    model = make_admission()
    assert InputAdmissionRecord.model_validate(model.model_dump(mode="json")) == model
    with pytest.raises(ValidationError):
        InputAdmissionRecord.model_validate({**model.model_dump(), "extra": 1})


def make_inbox(state="queued", **changes):
    data = dict(
        inbox_item_id=new_cycle_inbox_item_id(),
        admission_id=new_input_admission_id(), session_id="s",
        cycle_id="c", input_batch_id="b", cycle_sequence=1,
        generation=0, state=state, enqueued_at=NOW,
    )
    data.update(changes)
    return CycleInboxItem(**data)


def test_inbox_claims_and_contiguous_ranges():
    make_inbox()
    claimed = make_inbox("claimed", claim_token="t", claimed_at=NOW, claim_expires_at=NOW + timedelta(seconds=1))
    make_inbox("applying", claim_token="t", claimed_at=NOW, claim_expires_at=NOW + timedelta(seconds=1))
    make_inbox("applied", applied_at=NOW)
    with pytest.raises(ValidationError):
        make_inbox("claimed")
    with pytest.raises(ValidationError):
        make_inbox(claim_token="t")
    second = claimed.model_copy(update={"inbox_item_id": new_cycle_inbox_item_id(), "cycle_sequence": 3})
    with pytest.raises(ValidationError):
        ClaimedInboxRange(claim_token="t", session_id="s", cycle_id="c", generation=0, items=[claimed, second], first_sequence=1, last_sequence=3, total_items=2, estimated_total_bytes=0, claimed_at=NOW, claim_expires_at=NOW + timedelta(seconds=1))


def test_controls_revisions_emissions_and_finalization():
    base = dict(control_id=new_session_control_id(), session_id="s", generation=0, sequence_number=1, state="queued", idempotency_key="k", source_client_type="telegram", created_at=NOW)
    with pytest.raises(ValidationError):
        SessionControlCommand(command="pause", **base)
    SessionControlCommand(command="reset", **base)
    context_id = new_context_revision_id()
    CycleContextRevision(context_revision_id=context_id, cycle_id="c", session_id="s", revision_number=1, reason="initial_input", applied_through_cycle_sequence=0, created_at=NOW)
    with pytest.raises(ValidationError):
        CycleContextRevision(context_revision_id=new_context_revision_id(), cycle_id="c", session_id="s", revision_number=2, parent_revision_ids=[], reason="resumed", applied_through_cycle_sequence=0, created_at=NOW)
    with pytest.raises(ValidationError):
        AgentEmission(emission_id=new_agent_emission_id(), session_id="s", cycle_id="c", context_revision_id=context_id, kind="intermediate", text=" ", visibility="internal", importance="normal", response_route={}, state="ready", idempotency_key="k", created_at=NOW)
    CycleFinalizationRecord(finalization_id=new_finalization_id(), session_id="s", cycle_id="c", generation=0, context_revision_id=context_id, expected_accepted_sequence=1, expected_applied_sequence=1, expected_control_sequence=0, state="prepared", created_at=NOW, updated_at=NOW)
    with pytest.raises(ValidationError):
        CycleFinalizationRecord(finalization_id=new_finalization_id(), session_id="s", cycle_id="c", generation=0, context_revision_id=context_id, expected_accepted_sequence=2, expected_applied_sequence=1, expected_control_sequence=0, state="prepared", created_at=NOW, updated_at=NOW)
