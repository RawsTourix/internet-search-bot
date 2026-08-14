import asyncio,os
from datetime import datetime,timezone
from pathlib import Path
import pytest
from src.input_runtime import AdmissionKind,ActiveCycleSnapshot,AgentEmission,ControlCommandType,CycleFinalizationRecord,EmissionState,FinalizationState,SessionControlCommand,CheckpointName,CycleContextRevision,CycleStatus,InputAdmissionRecord,SessionInputRuntimeState,create_filesystem_input_runtime_repositories,new_context_revision_id
from src.input_runtime.emissions import AgentEmissionDeliveryReceipt
from src.input_runtime.errors import InputRuntimeConflictError,InputRuntimeError
from src.input_runtime.serialization import atomic_write_model,storage_key
from src.storage import StorageConfigType
NOW=datetime(2026,8,5,tzinfo=timezone.utc)
def run(c):return asyncio.run(c)
def repositories(p:Path):return create_filesystem_input_runtime_repositories(storage_config=StorageConfigType(root_dir=str(p)))
def state(session_id="session",revision=1):return SessionInputRuntimeState(session_id=session_id,generation=0,revision=revision,created_at=NOW,updated_at=NOW)
def admission(*,batch="batch",session_sequence=1,cycle_sequence=0,cycle="cycle",session="session",payload=0,admission_id=None):
 d=dict(session_id=session,input_batch_id=batch,session_sequence=session_sequence,target_cycle_id=cycle,cycle_sequence=cycle_sequence,admitted_generation=0,payload_size_bytes=payload,admission_kind=AdmissionKind.START_CYCLE if cycle_sequence==0 else AdmissionKind.CONTINUE_RUNNING,idempotency_key=f"key-{batch}",admitted_at=NOW)
 if admission_id:d["admission_id"]=admission_id
 return InputAdmissionRecord(**d)
def snapshot(*,cycle="cycle",revision=1,applied=0,batches=None,status=CycleStatus.RUNNING):return ActiveCycleSnapshot(cycle_id=cycle,session_id="session",generation=0,status=status,original_input_batch_id="batch",original_user_request="request",applied_input_batch_ids=list(batches or []),applied_through_cycle_sequence=applied,active_context_revision_id=new_context_revision_id(),snapshot_revision=revision,safe_checkpoint=CheckpointName.BEFORE_LLM,created_at=NOW,updated_at=NOW)
def test_session_state_cas_and_restart(tmp_path):
 r=repositories(tmp_path);assert run(r.sessions.create_if_absent(state())).revision==1;u=state(revision=2);assert run(r.sessions.compare_and_swap(1,u)).revision==2
 with pytest.raises(InputRuntimeConflictError):run(r.sessions.compare_and_swap(1,state(revision=2)))
 assert run(repositories(tmp_path).sessions.get("session"))==u
def test_coordinated_admission_allocation_updates_watermarks(tmp_path):
 r=repositories(tmp_path);run(r.sessions.create_if_absent(state()))
 first=run(r.admissions.allocate(admission()));assert(first.session_sequence,first.cycle_sequence)==(1,0)
 second=run(r.admissions.allocate(admission(batch="batch-2",cycle_sequence=1)));assert(second.session_sequence,second.cycle_sequence)==(2,1)
 s=run(r.sessions.get("session"));assert(s.accepted_through_session_sequence,s.active_cycle_accepted_through_sequence)==(2,1)
 assert run(repositories(tmp_path).admissions.allocate(admission(batch="batch-2",session_sequence=99,cycle_sequence=9)))==second
def test_context_revision_append_linear_restart_safe(tmp_path):
 r=repositories(tmp_path);a=CycleContextRevision(cycle_id="cycle",session_id="session",revision_number=1,reason="initial_input",created_at=NOW);run(r.context_revisions.append_revision(a));b=CycleContextRevision(cycle_id="cycle",session_id="session",revision_number=2,parent_revision_ids=[a.context_revision_id],reason="input_applied",created_at=NOW);run(r.context_revisions.append_revision(b));assert run(repositories(tmp_path).context_revisions.get_latest("cycle"))==b
def test_snapshot_cas_and_two_instances(tmp_path):
 a=repositories(tmp_path);b=repositories(tmp_path);run(a.snapshots.create_if_absent(snapshot()));u=snapshot(revision=2);assert run(b.snapshots.compare_and_swap(1,u))==u
 with pytest.raises(InputRuntimeConflictError):run(a.snapshots.compare_and_swap(1,u))
def test_user_ids_hashed(tmp_path):
 bad="../../outside/session";run(repositories(tmp_path).sessions.create_if_absent(state(session_id=bad)));assert(tmp_path/"input-runtime"/"sessions"/storage_key(bad)/"state.json").exists();assert not(tmp_path.parent/"outside").exists()
def test_atomic_write_failure_preserves_previous(tmp_path,monkeypatch):
 p=tmp_path/"state.json";atomic_write_model(p,state());monkeypatch.setattr(os,"replace",lambda *_:(_ for _ in ()).throw(OSError("boom")))
 with pytest.raises(InputRuntimeError):atomic_write_model(p,state(revision=2))
 from src.input_runtime.serialization import read_model
 assert read_model(p,SessionInputRuntimeState)==state()
def test_control_idempotent_restart_safe(tmp_path):
 r=repositories(tmp_path);c=SessionControlCommand(session_id="session",target_cycle_id="cycle",generation=0,sequence_number=1,command=ControlCommandType.PAUSE,idempotency_key="control-key",source_client_type="telegram",created_at=NOW);run(r.controls.append(c));dup=c.model_copy(update={"control_id":"ctl_"+"1"*32});assert run(r.controls.append(dup))==c;run(r.controls.acknowledge(c.control_id,acknowledged_at=NOW));assert run(repositories(tmp_path).controls.apply(c.control_id,applied_at=NOW)).applied_at==NOW
def test_emission_delivery_claim_fenced_and_durable(tmp_path):
 r=repositories(tmp_path);revision=new_context_revision_id();active=SessionInputRuntimeState(session_id="session",generation=0,active_cycle_id="cycle",cycle_status=CycleStatus.RUNNING,active_context_revision_id=revision,created_at=NOW,updated_at=NOW);run(r.sessions.create_if_absent(active));e=AgentEmission(session_id="session",cycle_id="cycle",generation=0,context_revision_id=revision,kind="intermediate",text="message",response_route={"client_type":"telegram","client_instance_id":"bot-a","conversation_id":"100","thread_id":None},idempotency_key="emission-key",created_at=NOW);run(r.emissions.create_if_absent(e));claimed=run(r.emissions.claim_delivery(e.emission_id,claim_token="claim",claimed_at=NOW));assert claimed.state==EmissionState.DELIVERING and claimed.delivery_claim_token=="claim"
 with pytest.raises(InputRuntimeConflictError):run(repositories(tmp_path).emissions.complete_delivery(e.emission_id,claim_token="stale",delivered_at=NOW))
 receipt=AgentEmissionDeliveryReceipt(emission_id=claimed.emission_id,session_id="session",cycle_id="cycle",generation=0,claim_token="claim",attempt_number=1,client_type="telegram",client_instance_id="bot-a",conversation_id="100",external_message_id="900",delivered_at=NOW);assert run(repositories(tmp_path).emissions.record_delivery_receipt(receipt)).state==EmissionState.DELIVERED
def test_finalization_transition_state_fenced(tmp_path):
 r=repositories(tmp_path);p=CycleFinalizationRecord(session_id="session",cycle_id="cycle",generation=0,context_revision_id=new_context_revision_id(),expected_accepted_sequence=1,expected_applied_sequence=1,expected_control_sequence=0,state=FinalizationState.PREPARED,created_at=NOW,updated_at=NOW);run(r.finalizations.prepare(p));n=CycleFinalizationRecord.model_validate({**p.model_dump(mode="json"),"state":FinalizationState.RESULT_PERSISTED,"result_ref":"result"});assert run(r.finalizations.advance(p.finalization_id,expected_state=FinalizationState.PREPARED,next_record=n))==n
 with pytest.raises(InputRuntimeConflictError):run(r.finalizations.advance(p.finalization_id,expected_state=FinalizationState.PREPARED,next_record=n))