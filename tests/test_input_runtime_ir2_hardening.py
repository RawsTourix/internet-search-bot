import asyncio
from datetime import datetime,timedelta,timezone
from pathlib import Path
import pytest
from src.input_runtime import ActiveCycleSnapshot,AdmissionKind,AgentEmission,CheckpointName,CycleFinalizationRecord,CycleInboxItem,CycleStatus,EmissionState,FinalizationState,InboxState,InputAdmissionRecord,SessionInputRuntimeState,create_filesystem_input_runtime_repositories,new_admission_id,new_context_revision_id
from src.input_runtime.coordination import SessionLockRegistry
from src.input_runtime.errors import InputRuntimeConflictError
from src.input_runtime.serialization import storage_key
from src.storage import StorageConfigType
NOW=datetime(2026,8,5,tzinfo=timezone.utc)
def repos(p):return create_filesystem_input_runtime_repositories(storage_config=StorageConfigType(root_dir=str(p)))
def session(s="s"):return SessionInputRuntimeState(session_id=s,generation=0,created_at=NOW,updated_at=NOW)
def adm(batch,*,cycle="c",session_id="s",kind=AdmissionKind.CONTINUE_RUNNING,aid=None,payload=0):
 d=dict(session_id=session_id,input_batch_id=batch,session_sequence=1,target_cycle_id=cycle,cycle_sequence=0 if kind==AdmissionKind.START_CYCLE else 1,admitted_generation=0,payload_size_bytes=payload,admission_kind=kind,idempotency_key=f"key-{batch}",admitted_at=NOW)
 if aid:d["admission_id"]=aid
 return InputAdmissionRecord(**d)
def snap(c,status=CycleStatus.RUNNING,revision=1):
 kw={}
 if status==CycleStatus.DONE:kw={}
 return ActiveCycleSnapshot(cycle_id=c,session_id="s",generation=0,status=status,original_input_batch_id="b",original_user_request="r",active_context_revision_id=new_context_revision_id(),snapshot_revision=revision,safe_checkpoint=CheckpointName.BEFORE_LLM,created_at=NOW,updated_at=NOW,**kw)
@pytest.mark.asyncio
async def test_lock_registry_never_evicts_referenced_new_session_lock(tmp_path):
 reg=SessionLockRegistry(max_entries=1);release=asyncio.Event();entered_a=asyncio.Event();active=0;maximum=0
 async def occupy():
  async with reg.hold(tmp_path,"a"):
   entered_a.set();await release.wait()
 async def worker():
  nonlocal active,maximum
  async with reg.hold(tmp_path,"b"):
   active+=1;maximum=max(maximum,active);await asyncio.sleep(0.03);active-=1
 task=asyncio.create_task(occupy());await entered_a.wait();await asyncio.gather(worker(),worker());release.set();await task
 assert maximum==1
@pytest.mark.asyncio
async def test_concurrent_allocation_has_no_duplicates_gaps_and_survives_restart(tmp_path):
 one,two=repos(tmp_path),repos(tmp_path);await one.sessions.create_if_absent(session());initial=await one.admissions.allocate(adm("initial",kind=AdmissionKind.START_CYCLE));assert initial.session_sequence==1
 async def allocate(i):return await (one if i%2 else two).admissions.allocate(adm(f"b{i}"))
 rows=await asyncio.gather(*(allocate(i) for i in range(1,21)))
 assert sorted(x.session_sequence for x in rows)==list(range(2,22));assert sorted(x.cycle_sequence for x in rows)==list(range(1,21))
 duplicate=await two.admissions.allocate(adm("b10"));assert duplicate==next(x for x in rows if x.input_batch_id=="b10")
 nxt=await repos(tmp_path).admissions.allocate(adm("after-restart"));assert(nxt.session_sequence,nxt.cycle_sequence)==(22,21)
@pytest.mark.asyncio
async def test_cross_bundle_duplicate_identity_and_stable_id_collisions(tmp_path):
 one,two=repos(tmp_path),repos(tmp_path);await one.sessions.create_if_absent(session());await one.admissions.allocate(adm("initial",kind=AdmissionKind.START_CYCLE))
 results=await asyncio.gather(one.admissions.allocate(adm("dup")),two.admissions.allocate(adm("dup")));assert results[0]==results[1]
 with pytest.raises(InputRuntimeConflictError):await two.admissions.create_if_absent(adm("dup",session_id="other",cycle="other"))
 fixed=new_admission_id();await one.admissions.create_if_absent(adm("fixed-a",aid=fixed))
 with pytest.raises(InputRuntimeConflictError):await two.admissions.create_if_absent(adm("fixed-b",aid=fixed))
 first=CycleInboxItem(admission_id=results[0].admission_id,session_id="s",cycle_id="c",input_batch_id="dup",cycle_sequence=1,generation=0,enqueued_at=NOW)
 await one.inbox.create_if_absent(first)
 with pytest.raises(InputRuntimeConflictError):await two.inbox.create_if_absent(first.model_copy(update={"inbox_item_id":"inbx_"+"2"*32,"cycle_id":"other"}))
@pytest.mark.asyncio
async def test_payload_byte_bounds_use_batch_metadata_not_record_json(tmp_path):
 r=repos(tmp_path);a1=adm("small",payload=5);a2=adm("large",payload=1000);i1=CycleInboxItem(admission_id=a1.admission_id,session_id="s",cycle_id="c",input_batch_id="small",cycle_sequence=1,generation=0,payload_size_bytes=5,enqueued_at=NOW);i2=CycleInboxItem(admission_id=a2.admission_id,session_id="s",cycle_id="c",input_batch_id="large",cycle_sequence=2,generation=0,payload_size_bytes=1000,enqueued_at=NOW);await r.inbox.create_if_absent(i1);await r.inbox.create_if_absent(i2)
 claim=await r.inbox.claim_contiguous_range("c",generation=0,after_sequence=0,max_items=8,max_bytes=10,lease_seconds=30);assert claim.claimed_bytes==5 and len(claim.items)==1
 await r.inbox.mark_applied(claim,applied_at=NOW);assert await r.inbox.claim_contiguous_range("c",generation=0,after_sequence=1,max_items=8,max_bytes=10,lease_seconds=30) is None
@pytest.mark.asyncio
async def test_snapshot_generation_cancellation_all_resumable_rereads_and_preserves_terminal(tmp_path):
 r=repos(tmp_path);await r.snapshots.create_if_absent(snap("c1"));await r.snapshots.create_if_absent(snap("c2"));terminal=snap("done",CycleStatus.DONE);await r.snapshots.create_if_absent(terminal)
 updated=snap("c1",revision=2);await r.snapshots.compare_and_swap(1,updated)
 changed=await r.snapshots.cancel_generation("s",generation=0,reason_code="reset");assert {x.cycle_id for x in changed}=={"c1","c2"};assert next(x for x in changed if x.cycle_id=="c1").snapshot_revision==3;assert(await r.snapshots.get("done")).status==CycleStatus.DONE;assert all(x.cancellation_reason_code=="reset" for x in changed)
@pytest.mark.asyncio
async def test_emission_generation_cancel_and_delivery_recovery(tmp_path):
 r=repos(tmp_path)
 def emission(eid,g,state=EmissionState.READY,**kw):return AgentEmission(emission_id=eid,session_id="s",cycle_id=f"c{g}",generation=g,context_revision_id=new_context_revision_id(),kind="intermediate",text="x",response_route={},state=state,idempotency_key=eid,created_at=NOW,**kw)
 ready0=emission("emit_"+"1"*32,0);ready1=emission("emit_"+"2"*32,1);legacy=emission("emit_"+"3"*32,0,EmissionState.DELIVERING);await r.emissions.create_if_absent(ready0);await r.emissions.create_if_absent(ready1);await r.emissions.create_if_absent(legacy)
 cancelled=await r.emissions.cancel_generation("s",generation=0,reason_code="reset");assert [x.emission_id for x in cancelled]==[ready0.emission_id] and cancelled[0].cancellation_reason_code=="reset";assert(await r.emissions.get_by_idempotency_key("c1",ready1.idempotency_key)).state==EmissionState.READY
 recovered=await r.emissions.recover_expired_delivery_claims(now=NOW);assert recovered[0].state==EmissionState.UNKNOWN and recovered[0].error_code=="delivery_claim_missing";assert legacy not in await r.emissions.list_pending_delivery()
 exp=emission("emit_"+"4"*32,0);await r.emissions.create_if_absent(exp);claimed=await r.emissions.claim_delivery(exp.emission_id,claim_token="old",claimed_at=NOW-timedelta(minutes=10),lease_seconds=1);await r.emissions.recover_expired_delivery_claims(now=NOW)
 with pytest.raises(InputRuntimeConflictError):await r.emissions.complete_delivery(exp.emission_id,claim_token="old",delivered_at=NOW)
@pytest.mark.asyncio
async def test_finalization_cancel_rereads_preserves_identity_and_terminal(tmp_path):
 r=repos(tmp_path)
 def prepared(fid,cycle):return CycleFinalizationRecord(finalization_id=fid,session_id="s",cycle_id=cycle,generation=0,context_revision_id=new_context_revision_id(),expected_accepted_sequence=1,expected_applied_sequence=1,expected_control_sequence=0,state=FinalizationState.PREPARED,created_at=NOW,updated_at=NOW)
 p=prepared("fin_"+"1"*32,"c1");await r.finalizations.prepare(p);advanced=CycleFinalizationRecord.model_validate({**p.model_dump(mode="json"),"state":FinalizationState.RESULT_PERSISTED,"result_ref":"r"});await r.finalizations.advance(p.finalization_id,expected_state=FinalizationState.PREPARED,next_record=advanced)
 t=prepared("fin_"+"2"*32,"c2");await r.finalizations.prepare(t);terminal=CycleFinalizationRecord.model_validate({**t.model_dump(mode="json"),"state":FinalizationState.TERMINAL_COMMITTED,"result_ref":"r"});await r.finalizations.advance(t.finalization_id,expected_state=FinalizationState.PREPARED,next_record=terminal)
 changed=await r.finalizations.cancel_generation("s",generation=0,reason_code="reset");assert len(changed)==1 and changed[0].state==FinalizationState.ABORTED_CONTROL and changed[0].cancellation_reason_code=="reset";assert(await r.finalizations.get(t.finalization_id)).state==FinalizationState.TERMINAL_COMMITTED
@pytest.mark.asyncio
async def test_missing_indexes_are_repaired_on_hot_lookup(tmp_path):
 r=repos(tmp_path);await r.sessions.create_if_absent(session());record=await r.admissions.allocate(adm("initial",kind=AdmissionKind.START_CYCLE));idx=tmp_path/"input-runtime"/"indexes"/"admission-by-input"/f"{storage_key('initial')}.json";assert idx.exists();idx.unlink();assert await repos(tmp_path).admissions.get_by_input_batch_id("initial")==record;assert idx.exists()
