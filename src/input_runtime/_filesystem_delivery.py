"""Emission and finalization filesystem repositories."""
from __future__ import annotations
from datetime import datetime,timedelta,timezone
from .errors import InputRuntimeConflictError,InputRuntimeNotFoundError
from .models import AgentEmission,CycleFinalizationRecord,EmissionState,FinalizationState
from .serialization import atomic_write_model,read_model
from ._filesystem_common import _RepositoryBase,validated_copy

def _same_emission_relation(a:AgentEmission,b:AgentEmission)->bool:
    return (a.session_id,a.cycle_id,a.generation,a.context_revision_id,a.kind,a.text,a.visibility,a.importance,a.response_route,a.idempotency_key)==(b.session_id,b.cycle_id,b.generation,b.context_revision_id,b.kind,b.text,b.visibility,b.importance,b.response_route,b.idempotency_key)
def _final_identity(r:CycleFinalizationRecord)->tuple:
    return (r.finalization_id,r.session_id,r.cycle_id,r.generation,r.context_revision_id,r.expected_accepted_sequence,r.expected_applied_sequence,r.expected_control_sequence)

class FileSystemAgentEmissionRepository(_RepositoryBase):
    def _scan(self)->tuple[AgentEmission,...]:
        d=self.layout.root/"cycles";return tuple(read_model(p,AgentEmission) for p in sorted(d.glob("*/emissions/*.json"))) if d.exists() else ()
    def _index(self,r:AgentEmission)->None:self._write_pointer(self.layout.record_index("emission",r.emission_id),self._pointer("emission",r.emission_id,r.session_id,self.layout.emission(r.cycle_id,r.emission_id),r.cycle_id));self._ensure_cycle_authority(r.cycle_id,r.session_id)
    async def _find(self,emission_id:str)->AgentEmission:
        ptr=self._read_pointer(self.layout.record_index("emission",emission_id))
        if ptr and self._pointer_record_path(ptr).exists():return read_model(self._pointer_record_path(ptr),AgentEmission)
        rows=[x for x in self._scan() if x.emission_id==emission_id]
        if not rows:raise InputRuntimeNotFoundError(emission_id)
        if len(rows)>1:raise InputRuntimeConflictError("duplicate emission stable ID")
        self._index(rows[0]);return rows[0]
    async def create_if_absent(self,e:AgentEmission)->AgentEmission:
        async with self.locks.hold(self.root,e.session_id):
            existing=await self.get_by_idempotency_key(e.cycle_id,e.idempotency_key)
            if existing is not None:
                if not _same_emission_relation(existing,e):raise InputRuntimeConflictError("emission idempotency relation changed")
                return existing
            try:by_id=await self._find(e.emission_id)
            except InputRuntimeNotFoundError:by_id=None
            if by_id is not None:
                if by_id!=e:raise InputRuntimeConflictError("emission stable ID collision")
                return by_id
            self._ensure_cycle_authority(e.cycle_id,e.session_id);atomic_write_model(self.layout.emission(e.cycle_id,e.emission_id),e);self._index(e);return e
    async def get_by_idempotency_key(self,cycle_id:str,idempotency_key:str)->AgentEmission|None:
        return next((x for x in self._scan() if x.cycle_id==cycle_id and x.idempotency_key==idempotency_key.strip()),None)
    async def claim_delivery(self,emission_id:str,*,claim_token:str,claimed_at:datetime|None=None,lease_seconds:int=300)->AgentEmission:
        stale=await self._find(emission_id);token=claim_token.strip()
        if not token or lease_seconds<=0:raise ValueError("invalid delivery claim")
        now=claimed_at or datetime.now(timezone.utc)
        async with self.locks.hold(self.root,stale.session_id):
            cur=await self._find(emission_id)
            if cur.state!=EmissionState.READY:raise InputRuntimeConflictError("emission is not ready")
            u=validated_copy(cur,state=EmissionState.DELIVERING,delivery_claim_token=token,delivery_claimed_at=now,delivery_claim_expires_at=now+timedelta(seconds=lease_seconds),delivery_attempt_count=cur.delivery_attempt_count+1)
            atomic_write_model(self.layout.emission(cur.cycle_id,cur.emission_id),u);return u
    async def complete_delivery(self,emission_id:str,*,claim_token:str,delivered_at:datetime)->AgentEmission:
        stale=await self._find(emission_id)
        async with self.locks.hold(self.root,stale.session_id):
            cur=await self._find(emission_id)
            if cur.state!=EmissionState.DELIVERING or cur.delivery_claim_token!=claim_token.strip():raise InputRuntimeConflictError("stale emission delivery claim")
            u=validated_copy(cur,state=EmissionState.DELIVERED,delivered_at=delivered_at,error_code=None,delivery_claim_token=None,delivery_claimed_at=None,delivery_claim_expires_at=None)
            atomic_write_model(self.layout.emission(cur.cycle_id,cur.emission_id),u);return u
    async def fail_delivery(self,emission_id:str,*,claim_token:str,state:str,error_code:str)->AgentEmission:
        next_state=EmissionState(state)
        if next_state not in {EmissionState.FAILED,EmissionState.UNKNOWN}:raise ValueError("delivery failure state must be failed or unknown")
        stale=await self._find(emission_id)
        async with self.locks.hold(self.root,stale.session_id):
            cur=await self._find(emission_id)
            if cur.state!=EmissionState.DELIVERING or cur.delivery_claim_token!=claim_token.strip():raise InputRuntimeConflictError("stale emission delivery claim")
            u=validated_copy(cur,state=next_state,error_code=error_code,delivery_claim_token=None,delivery_claimed_at=None,delivery_claim_expires_at=None)
            atomic_write_model(self.layout.emission(cur.cycle_id,cur.emission_id),u);return u
    async def recover_expired_delivery_claims(self,*,now:datetime)->tuple[AgentEmission,...]:
        out=[]
        for stale in self._scan():
            if stale.state!=EmissionState.DELIVERING:continue
            missing=not all((stale.delivery_claim_token,stale.delivery_claimed_at,stale.delivery_claim_expires_at))
            expired=stale.delivery_claim_expires_at is not None and stale.delivery_claim_expires_at<=now
            if not missing and not expired:continue
            async with self.locks.hold(self.root,stale.session_id):
                cur=await self._find(stale.emission_id)
                missing=not all((cur.delivery_claim_token,cur.delivery_claimed_at,cur.delivery_claim_expires_at));expired=cur.delivery_claim_expires_at is not None and cur.delivery_claim_expires_at<=now
                if cur.state!=EmissionState.DELIVERING or (not missing and not expired):continue
                u=validated_copy(cur,state=EmissionState.UNKNOWN,error_code="delivery_claim_missing" if missing else "delivery_claim_expired",delivery_claim_token=None,delivery_claimed_at=None,delivery_claim_expires_at=None)
                atomic_write_model(self.layout.emission(cur.cycle_id,cur.emission_id),u);out.append(u)
        return tuple(out)
    async def list_pending_delivery(self)->tuple[AgentEmission,...]:return tuple(x for x in self._scan() if x.state in {EmissionState.READY,EmissionState.DELIVERING})
    async def cancel_generation(self,session_id:str,*,generation:int,reason_code:str)->tuple[AgentEmission,...]:
        out=[]
        async with self.locks.hold(self.root,session_id):
            for stale in self._scan():
                if stale.session_id!=session_id:continue
                cur=await self._find(stale.emission_id)
                if cur.generation==generation and cur.state==EmissionState.READY:
                    u=validated_copy(cur,state=EmissionState.CANCELLED,cancellation_reason_code=reason_code);atomic_write_model(self.layout.emission(cur.cycle_id,cur.emission_id),u);out.append(u)
        return tuple(out)

class FileSystemFinalizationRepository(_RepositoryBase):
    def _scan(self)->tuple[CycleFinalizationRecord,...]:
        d=self.layout.root/"cycles";return tuple(read_model(p,CycleFinalizationRecord) for p in sorted(d.glob("*/finalizations/*.json"))) if d.exists() else ()
    def _index(self,r:CycleFinalizationRecord)->None:self._write_pointer(self.layout.record_index("finalization",r.finalization_id),self._pointer("finalization",r.finalization_id,r.session_id,self.layout.finalization(r.cycle_id,r.finalization_id),r.cycle_id));self._ensure_cycle_authority(r.cycle_id,r.session_id)
    async def get(self,finalization_id:str)->CycleFinalizationRecord|None:
        ptr=self._read_pointer(self.layout.record_index("finalization",finalization_id))
        if ptr and self._pointer_record_path(ptr).exists():return read_model(self._pointer_record_path(ptr),CycleFinalizationRecord)
        rows=[x for x in self._scan() if x.finalization_id==finalization_id]
        if len(rows)>1:raise InputRuntimeConflictError("duplicate finalization stable ID")
        if rows:self._index(rows[0]);return rows[0]
        return None
    async def prepare(self,r:CycleFinalizationRecord)->CycleFinalizationRecord:
        if r.state!=FinalizationState.PREPARED:raise ValueError("prepare requires PREPARED")
        async with self.locks.hold(self.root,r.session_id):
            current=await self.get(r.finalization_id)
            if current is not None:
                if current!=r:raise InputRuntimeConflictError("finalization stable ID collision")
                return current
            self._ensure_cycle_authority(r.cycle_id,r.session_id);atomic_write_model(self.layout.finalization(r.cycle_id,r.finalization_id),r);self._index(r);return r
    async def _transition(self,finalization_id:str,*,expected_state:str,next_record:CycleFinalizationRecord)->CycleFinalizationRecord:
        stale=await self.get(finalization_id)
        if stale is None:raise InputRuntimeNotFoundError(finalization_id)
        async with self.locks.hold(self.root,stale.session_id):
            cur=await self.get(finalization_id)
            if cur is None:raise InputRuntimeNotFoundError(finalization_id)
            if cur.state!=FinalizationState(expected_state):raise InputRuntimeConflictError("stale finalization state")
            if _final_identity(cur)!=_final_identity(next_record):raise InputRuntimeConflictError("finalization immutable identity changed")
            terminal={FinalizationState.TERMINAL_COMMITTED,FinalizationState.ABORTED_NEW_INPUT,FinalizationState.ABORTED_CONTROL,FinalizationState.FAILED_TERMINAL}
            if cur.state in terminal:raise InputRuntimeConflictError("terminal finalization cannot transition")
            atomic_write_model(self.layout.finalization(cur.cycle_id,cur.finalization_id),next_record);return next_record
    async def advance(self,finalization_id:str,*,expected_state:str,next_record:CycleFinalizationRecord)->CycleFinalizationRecord:return await self._transition(finalization_id,expected_state=expected_state,next_record=next_record)
    async def abort(self,finalization_id:str,*,expected_state:str,next_record:CycleFinalizationRecord)->CycleFinalizationRecord:
        if next_record.state not in {FinalizationState.ABORTED_NEW_INPUT,FinalizationState.ABORTED_CONTROL,FinalizationState.FAILED_RECOVERABLE,FinalizationState.FAILED_TERMINAL}:raise ValueError("invalid abort state")
        return await self._transition(finalization_id,expected_state=expected_state,next_record=next_record)
    async def list_recoverable(self)->tuple[CycleFinalizationRecord,...]:return tuple(x for x in self._scan() if x.state in {FinalizationState.PREPARED,FinalizationState.RESULT_PERSISTED,FinalizationState.OUTPUT_READY,FinalizationState.FAILED_RECOVERABLE})
    async def cancel_generation(self,session_id:str,*,generation:int,reason_code:str)->tuple[CycleFinalizationRecord,...]:
        out=[]
        async with self.locks.hold(self.root,session_id):
            for stale in self._scan():
                if stale.session_id!=session_id:continue
                cur=await self.get(stale.finalization_id)
                if cur is None:continue
                if cur.generation==generation and cur.state in {FinalizationState.PREPARED,FinalizationState.RESULT_PERSISTED,FinalizationState.OUTPUT_READY,FinalizationState.FAILED_RECOVERABLE}:
                    u=validated_copy(cur,state=FinalizationState.ABORTED_CONTROL,cancellation_reason_code=reason_code,failure_code=None,updated_at=datetime.now(timezone.utc));atomic_write_model(self.layout.finalization(cur.cycle_id,cur.finalization_id),u);out.append(u)
        return tuple(out)
