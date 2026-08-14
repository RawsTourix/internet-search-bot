from datetime import datetime,timedelta,timezone
from enum import Enum
import pytest
from pydantic import ValidationError
from src.input_runtime.models import ActiveCycleSnapshot,AdmissionKind,AdmissionState,AgentEmission,CheckpointAction,CheckpointName,CheckpointOutcome,ClaimedInboxRange,ControlCommandType,ControlOutcome,ControlState,CycleContextRevision,CycleFinalizationRecord,CycleInboxItem,CycleStatus,EmissionState,FinalizationState,InboxState,InputAdmissionOutcome,InputAdmissionRecord,SessionControlCommand,SessionInputRuntimeState,new_admission_id,new_context_revision_id,new_control_id,new_emission_id,new_finalization_id,new_inbox_item_id
NOW=datetime(2026,8,5,tzinfo=timezone.utc);LATER=NOW+timedelta(minutes=5)
def admission(**o):
 d=dict(session_id=" session ",input_batch_id=" ibat ",session_sequence=1,target_cycle_id=" cycle ",cycle_sequence=0,admitted_generation=0,admission_kind=AdmissionKind.START_CYCLE,idempotency_key=" key ",admitted_at=NOW);d.update(o);return InputAdmissionRecord(**d)
def inbox(state=InboxState.QUEUED,**o):
 d=dict(admission_id=new_admission_id(),session_id="s",cycle_id="c",input_batch_id="ibat",cycle_sequence=1,generation=0,state=state,enqueued_at=NOW)
 if state in {InboxState.CLAIMED,InboxState.APPLYING}:d.update(claim_token="claim",claimed_at=NOW,claim_expires_at=LATER)
 if state==InboxState.APPLIED:d["applied_at"]=LATER
 if state==InboxState.CANCELLED:d.update(cancelled_at=LATER,last_error_code="reset")
 if state==InboxState.FAILED_TERMINAL:d["last_error_code"]="broken"
 d.update(o);return CycleInboxItem(**d)
def test_stable_ids_and_enums_are_public_contracts():
 for value,prefix in [(new_admission_id(),"adm_"),(new_inbox_item_id(),"inbx_"),(new_control_id(),"ctl_"),(new_context_revision_id(),"ctxrev_"),(new_emission_id(),"emit_"),(new_finalization_id(),"fin_")]:assert value.startswith(prefix) and len(value.removeprefix(prefix))==32 and value==value.lower()
 for t in (CycleStatus,CheckpointName,AdmissionState,InboxState,ControlState,EmissionState,FinalizationState):assert issubclass(t,str) and issubclass(t,Enum)
def test_session_defaults_and_terminal_watermarks():
 s=SessionInputRuntimeState(session_id="s",generation=0,created_at=NOW,updated_at=NOW);assert s.accepted_through_session_sequence==s.active_cycle_accepted_through_sequence==s.active_cycle_applied_through_sequence==0 and s.revision==1
 with pytest.raises(ValidationError):SessionInputRuntimeState(session_id="s",generation=0,active_cycle_id="c",cycle_status=CycleStatus.DONE,active_cycle_accepted_through_sequence=2,active_cycle_applied_through_sequence=1,created_at=NOW,updated_at=NOW)
@pytest.mark.parametrize("value",["","   "])
def test_required_identity_strings_are_normalized_and_nonempty(value):
 with pytest.raises(ValidationError):admission(session_id=value)
 r=admission();assert(r.session_id,r.target_cycle_id,r.idempotency_key)==("session","cycle","key")
def test_admission_sequence_state_and_payload_contracts():
 assert admission(payload_size_bytes=999).payload_size_bytes==999
 with pytest.raises(ValidationError):admission(session_sequence=0)
 with pytest.raises(ValidationError):admission(admission_kind=AdmissionKind.CONTINUE_RUNNING,cycle_sequence=0)
 with pytest.raises(ValidationError):admission(admission_kind=AdmissionKind.START_CYCLE,cycle_sequence=1)
 assert admission(state=AdmissionState.APPLIED,applied_at=LATER).state==AdmissionState.APPLIED
 with pytest.raises(ValidationError):admission(state=AdmissionState.CANCELLED,cancelled_at=LATER)
 assert admission(state=AdmissionState.CANCELLED,cancelled_at=LATER,cancellation_reason_code="reset").cancellation_reason_code=="reset"
def test_admission_outcomes_are_stateful():
 r=admission();assert InputAdmissionOutcome(outcome="start_cycle",admission=r).admission==r
 assert InputAdmissionOutcome(outcome="capacity_blocked",retryable=True,reason_code="capacity").admission is None
 with pytest.raises(ValidationError):InputAdmissionOutcome(outcome="capacity_blocked")
@pytest.mark.parametrize("state",list(InboxState))
def test_all_inbox_states_round_trip(state):
 item=inbox(state);assert CycleInboxItem.model_validate_json(item.model_dump_json())==item
def test_claim_range_is_contiguous_fenced_and_payload_sized():
 a=inbox(InboxState.CLAIMED,payload_size_bytes=7);b=inbox(InboxState.CLAIMED,cycle_sequence=2,payload_size_bytes=11)
 c=ClaimedInboxRange(cycle_id="c",generation=0,claim_token="claim",first_cycle_sequence=1,last_cycle_sequence=2,items=(a,b),claimed_bytes=18,claim_expires_at=LATER);assert c.claimed_bytes==18
 with pytest.raises(ValidationError):ClaimedInboxRange(cycle_id="c",generation=0,claim_token="claim",first_cycle_sequence=1,last_cycle_sequence=2,items=(a,b),claimed_bytes=1,claim_expires_at=LATER)
@pytest.mark.parametrize("state",list(ControlState))
def test_all_control_states(state):
 d=dict(session_id="s",target_cycle_id="c",generation=0,sequence_number=1,command=ControlCommandType.PAUSE,state=state,idempotency_key="k",source_client_type="telegram",created_at=NOW)
 if state in {ControlState.ACKNOWLEDGED,ControlState.APPLIED}:d["acknowledged_at"]=NOW
 if state==ControlState.APPLIED:d["applied_at"]=LATER
 if state==ControlState.REJECTED:d["rejection_code"]="invalid"
 if state==ControlState.CANCELLED:d["cancellation_reason_code"]="reset"
 r=SessionControlCommand(**d);assert SessionControlCommand.model_validate_json(r.model_dump_json())==r
def test_control_outcome_and_reset_target_policy():
 r=SessionControlCommand(session_id="s",generation=1,sequence_number=1,command=ControlCommandType.RESET,idempotency_key="k",source_client_type="web",created_at=NOW);assert r.target_cycle_id is None and ControlOutcome(outcome=ControlState.QUEUED,command=r).command==r
def snapshot(**o):
 d=dict(cycle_id="c",session_id="s",generation=0,status=CycleStatus.RUNNING,original_input_batch_id="ibat",original_user_request="request",active_context_revision_id=new_context_revision_id(),snapshot_revision=1,safe_checkpoint=CheckpointName.BEFORE_LLM,created_at=NOW,updated_at=NOW);d.update(o);return ActiveCycleSnapshot(**d)
def test_snapshot_enums_revision_context_and_cancel_reason():
 assert snapshot().snapshot_revision==1
 with pytest.raises(ValidationError):snapshot(snapshot_revision=0)
 with pytest.raises(ValidationError):snapshot(active_context_revision_id="ctxrev_bad")
 with pytest.raises(ValidationError):snapshot(status=CycleStatus.CANCELLED)
 assert snapshot(status=CycleStatus.CANCELLED,cancellation_reason_code="reset").status==CycleStatus.CANCELLED
@pytest.mark.parametrize("status,field",[(CycleStatus.WAITING_USER,"waiting_question"),(CycleStatus.PAUSED_BY_USER,"pause_reason"),(CycleStatus.INTERRUPTED,"interruption_reason")])
def test_snapshot_state_metadata(status,field):
 with pytest.raises(ValidationError):snapshot(status=status)
 assert snapshot(status=status,**{field:"reason"}).status==status
def test_context_revision_parent_ids_and_linear_order():
 a=CycleContextRevision(cycle_id="c",session_id="s",revision_number=1,reason="initial_input",created_at=NOW);b=CycleContextRevision(cycle_id="c",session_id="s",revision_number=2,parent_revision_ids=[a.context_revision_id],reason="input_applied",created_at=NOW);assert b.parent_revision_ids==[a.context_revision_id]
@pytest.mark.parametrize("state",list(EmissionState))
def test_all_emission_states(state):
 d=dict(session_id="s",cycle_id="c",generation=3,context_revision_id=new_context_revision_id(),kind="intermediate",text="hello",response_route={"client":"web"},state=state,idempotency_key="k",created_at=NOW)
 if state==EmissionState.DELIVERED:d["delivered_at"]=LATER
 if state in {EmissionState.FAILED,EmissionState.UNKNOWN}:d["error_code"]="delivery"
 if state==EmissionState.CANCELLED:d["cancellation_reason_code"]="reset"
 if state==EmissionState.DELIVERING:d.update(delivery_claim_token="token",delivery_claimed_at=NOW,delivery_claim_expires_at=LATER)
 e=AgentEmission(**d);assert AgentEmission.model_validate_json(e.model_dump_json())==e
@pytest.mark.parametrize("state",list(FinalizationState))
def test_all_finalization_states(state):
 d=dict(session_id="s",cycle_id="c",generation=0,context_revision_id=new_context_revision_id(),expected_accepted_sequence=1,expected_applied_sequence=1,expected_control_sequence=0,state=state,created_at=NOW,updated_at=NOW)
 if state in {FinalizationState.RESULT_PERSISTED,FinalizationState.OUTPUT_READY,FinalizationState.TERMINAL_COMMITTED}:d["result_ref"]="result"
 if state==FinalizationState.OUTPUT_READY:d["output_batch_id"]="output"
 if state in {FinalizationState.FAILED_RECOVERABLE,FinalizationState.FAILED_TERMINAL}:d["failure_code"]="failure"
 if state==FinalizationState.ABORTED_CONTROL:d["cancellation_reason_code"]="reset"
 r=CycleFinalizationRecord(**d);assert CycleFinalizationRecord.model_validate_json(r.model_dump_json())==r
def test_prepared_requires_equal_watermarks_and_terminal_output_optional():
 with pytest.raises(ValidationError):CycleFinalizationRecord(session_id="s",cycle_id="c",generation=0,context_revision_id=new_context_revision_id(),expected_accepted_sequence=2,expected_applied_sequence=1,expected_control_sequence=0,state=FinalizationState.PREPARED,created_at=NOW,updated_at=NOW)
 t=CycleFinalizationRecord(session_id="s",cycle_id="c",generation=0,context_revision_id=new_context_revision_id(),expected_accepted_sequence=1,expected_applied_sequence=1,expected_control_sequence=0,state=FinalizationState.TERMINAL_COMMITTED,result_ref="r",created_at=NOW,updated_at=NOW);assert t.output_batch_id is None
def test_checkpoint_enums_and_revision_validation():
 o=CheckpointOutcome(checkpoint=CheckpointName.AFTER_TOOL_BLOCK,action=CheckpointAction.INPUT_APPLIED,context_revision_id=new_context_revision_id(),applied_input_batch_ids=("ibat",),applied_through_cycle_sequence=1);assert CheckpointOutcome.model_validate_json(o.model_dump_json())==o
def test_naive_timestamp_and_json_unsafe_values_rejected():
 with pytest.raises(ValidationError):SessionInputRuntimeState(session_id="s",generation=0,created_at=datetime(2026,8,5),updated_at=NOW)
 with pytest.raises(ValidationError):AgentEmission(session_id="s",cycle_id="c",context_revision_id=new_context_revision_id(),kind="intermediate",text="hello",response_route={"bad":{1}},idempotency_key="k",created_at=NOW)
