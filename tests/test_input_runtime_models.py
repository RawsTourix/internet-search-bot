from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.input_runtime.models import (
    AgentEmission,
    CycleContextRevision,
    CycleFinalizationRecord,
    CycleInboxItem,
    InputAdmissionRecord,
    SessionInputRuntimeState,
    new_admission_id,
    new_context_revision_id,
    new_control_id,
    new_emission_id,
    new_finalization_id,
    new_inbox_item_id,
)

NOW = datetime(2026, 8, 5, tzinfo=timezone.utc)


def test_stable_ids_have_contract_prefix_and_lower_hex():
    for value, prefix in [
        (new_admission_id(), "adm_"),
        (new_inbox_item_id(), "inbx_"),
        (new_control_id(), "ctl_"),
        (new_context_revision_id(), "ctxrev_"),
        (new_emission_id(), "emit_"),
        (new_finalization_id(), "fin_"),
    ]:
        assert value.startswith(prefix)
        assert len(value.removeprefix(prefix)) == 32
        assert value == value.lower()


def test_terminal_session_requires_equal_watermarks():
    with pytest.raises(ValidationError):
        SessionInputRuntimeState(
            session_id="s", generation=0, active_cycle_id="c", cycle_status="done",
            accepted_through_session_sequence=2,
            active_cycle_accepted_through_sequence=2,
            active_cycle_applied_through_sequence=1,
            pending_control_sequence=0, applied_control_sequence=0,
            revision=1, created_at=NOW, updated_at=NOW,
        )


def test_naive_durable_timestamp_is_rejected():
    with pytest.raises(ValidationError):
        SessionInputRuntimeState(
            session_id="s", generation=0, cycle_status="idle",
            created_at=datetime(2026, 8, 5), updated_at=NOW,
        )


def test_initial_admission_uses_cycle_sequence_zero_and_round_trips():
    record = InputAdmissionRecord(
        session_id="s", input_batch_id="ibat", session_sequence=0,
        target_cycle_id="cycle", cycle_sequence=0, admitted_generation=0,
        admission_kind="start_cycle", idempotency_key="ibat", admitted_at=NOW,
    )
    assert InputAdmissionRecord.model_validate_json(record.model_dump_json()) == record
    with pytest.raises(ValidationError):
        record.model_copy(update={"admission_id": "client-message-1"})


def test_inbox_claim_fields_are_state_dependent():
    base = dict(
        admission_id=new_admission_id(), session_id="s", cycle_id="c",
        input_batch_id="ibat", cycle_sequence=1, generation=0, enqueued_at=NOW,
    )
    with pytest.raises(ValidationError):
        CycleInboxItem(**base, state="claimed", claim_token="token")
    queued = CycleInboxItem(**base)
    assert queued.claim_token is None


def test_context_revisions_are_linear_in_v04():
    initial = CycleContextRevision(
        cycle_id="c", session_id="s", revision_number=1,
        reason="initial_input", applied_input_batch_ids=["ibat"],
        applied_through_cycle_sequence=0, created_at=NOW,
    )
    with pytest.raises(ValidationError):
        CycleContextRevision(
            cycle_id="c", session_id="s", revision_number=2,
            parent_revision_ids=[initial.context_revision_id, new_context_revision_id()],
            reason="input_applied", applied_through_cycle_sequence=1, created_at=NOW,
        )


def test_json_unsafe_metadata_is_rejected():
    with pytest.raises(ValidationError):
        AgentEmission(
            session_id="s", cycle_id="c", context_revision_id=new_context_revision_id(),
            kind="intermediate", text="hello", response_route={"bad": {1, 2}},
            idempotency_key="k", created_at=NOW,
        )


def test_terminal_finalization_requires_consumed_input_and_output_refs():
    with pytest.raises(ValidationError):
        CycleFinalizationRecord(
            session_id="s", cycle_id="c", generation=0,
            context_revision_id=new_context_revision_id(),
            expected_accepted_sequence=2, expected_applied_sequence=1,
            expected_control_sequence=0, state="terminal_committed",
            result_ref="result", output_batch_id="output",
            created_at=NOW, updated_at=NOW,
        )
