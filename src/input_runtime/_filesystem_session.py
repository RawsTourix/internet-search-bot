"""Session-scoped filesystem repositories."""
from __future__ import annotations
from datetime import datetime
from .errors import InputRuntimeConflictError,InputRuntimeNotFoundError
from .models import AdmissionKind,AdmissionState,ControlState,CycleStatus,InputAdmissionRecord,SessionControlCommand,SessionInputRuntimeState
from .serialization import atomic_write_model,list_models,read_model
from ._filesystem_common import _RepositoryBase,validated_copy

def _same_admission_relation(a:InputAdmissionRecord,b:InputAdmissionRecord)->bool:
    return (a.session_id,a.input_batch_id,a.target_cycle_id,a.admission_kind,a.idempotency_key,a.admitted_generation,a.payload_size_bytes)==(b.session_id,b.input_batch_id,b.target_cycle_id,b.admission_kind,b.idempotency_key,b.admitted_generation,b.payload_size_bytes)
def _same_control_relation(a:SessionControlCommand,b:SessionControlCommand)->bool:
    return (a.session_id,a.target_cycle_id,a.generation,a.command,a.idempotency_key,a.source_client_type,a.source_message_ref)==(b.session_id,b.target_cycle_id,b.generation,b.command,b.idempotency_key,b.source_client_type,b.source_message_ref)

class FileSystemSessionInputRuntimeRepository(_RepositoryBase):
    async def create_if_absent(self,state:SessionInputRuntimeState)->SessionInputRuntimeState:
        async with self.locks.hold(self.root,state.session_id):
            p=self.layout.state(state.session_id)
            if p.exists():
                current=read_model(p,SessionInputRuntimeState)
                if current!=state:raise InputRuntimeConflictError("session state already exists with different content")
                return current
            atomic_write_model(p,state);return state
    async def get(self,session_id:str)->SessionInputRuntimeState|None:
        p=self.layout.state(session_id);return read_model(p,SessionInputRuntimeState) if p.exists() else None
    async def compare_and_swap(self,expected_revision:int,state:SessionInputRuntimeState)->SessionInputRuntimeState:
        async with self.locks.hold(self.root,state.session_id):
            p=self.layout.state(state.session_id)
            if not p.exists():raise InputRuntimeNotFoundError(state.session_id)
            cur=read_model(p,SessionInputRuntimeState)
            if cur.revision!=expected_revision or state.revision!=expected_revision+1:raise InputRuntimeConflictError("stale session state revision")
            atomic_write_model(p,state);return state
    async def list_states(self)->tuple[SessionInputRuntimeState,...]:
        d=self.layout.root/"sessions";rows=[read_model(p,SessionInputRuntimeState) for p in sorted(d.glob("*/state.json"))] if d.exists() else []
        return tuple(sorted(rows,key=lambda x:x.session_id))

class FileSystemInputAdmissionRepository(_RepositoryBase):
    def _scan(self)->tuple[InputAdmissionRecord,...]:
        d=self.layout.root/"sessions";return tuple(read_model(p,InputAdmissionRecord) for p in sorted(d.glob("*/admissions/*.json"))) if d.exists() else ()
    def _index_record(self,r:InputAdmissionRecord)->None:
        path=self.layout.admission(r.session_id,r.admission_id);ptr=self._pointer("admission",r.admission_id,r.session_id,path,r.target_cycle_id)
        self._write_pointer(self.layout.record_index("admission",r.admission_id),ptr);self._write_pointer(self.layout.admission_input(r.input_batch_id),ptr);self._ensure_cycle_authority(r.target_cycle_id,r.session_id)
    def _get_from_pointer(self,p)->InputAdmissionRecord|None:
        if p is None:return None
        path=self._pointer_record_path(p)
        return read_model(path,InputAdmissionRecord) if path.exists() else None
    async def get_by_input_batch_id(self,input_batch_id:str)->InputAdmissionRecord|None:
        p=self._read_pointer(self.layout.admission_input(input_batch_id));r=self._get_from_pointer(p)
        if r is not None:
            if r.input_batch_id!=input_batch_id:raise InputRuntimeConflictError("admission input index mismatch")
            return r
        matches=[x for x in self._scan() if x.input_batch_id==input_batch_id]
        if len(matches)>1:raise InputRuntimeConflictError("duplicate admissions for input batch")
        if matches:self._index_record(matches[0]);return matches[0]
        return None
    async def _find_id(self,admission_id:str)->InputAdmissionRecord:
        r=self._get_from_pointer(self._read_pointer(self.layout.record_index("admission",admission_id)))
        if r is None:
            rows=[x for x in self._scan() if x.admission_id==admission_id]
            if not rows:raise InputRuntimeNotFoundError(admission_id)
            if len(rows)>1:raise InputRuntimeConflictError("duplicate admission stable ID")
            r=rows[0];self._index_record(r)
        return r
    async def create_if_absent(self,record:InputAdmissionRecord)->InputAdmissionRecord:
        async with self.locks.hold(self.root,record.session_id):
            existing=await self.get_by_input_batch_id(record.input_batch_id)
            if existing is not None:
                if not _same_admission_relation(existing,record):raise InputRuntimeConflictError("input batch admission relation changed")
                return existing
            try:by_id=await self._find_id(record.admission_id)
            except InputRuntimeNotFoundError:by_id=None
            if by_id is not None:
                if by_id!=record:raise InputRuntimeConflictError("admission stable ID collision")
                return by_id
            rows=await self.list_for_session(record.session_id)
            if any(x.session_sequence==record.session_sequence for x in rows) or any(x.target_cycle_id==record.target_cycle_id and x.cycle_sequence==record.cycle_sequence for x in rows):raise InputRuntimeConflictError("duplicate admission sequence")
            self._ensure_cycle_authority(record.target_cycle_id,record.session_id)
            atomic_write_model(self.layout.admission(record.session_id,record.admission_id),record);self._index_record(record);return record
    async def allocate(self,record:InputAdmissionRecord)->InputAdmissionRecord:
        async with self.locks.hold(self.root,record.session_id):
            existing=await self.get_by_input_batch_id(record.input_batch_id)
            if existing is not None:
                if not _same_admission_relation(existing,record):raise InputRuntimeConflictError("input batch admission relation changed")
                return existing
            state_path=self.layout.state(record.session_id)
            if not state_path.exists():raise InputRuntimeNotFoundError("session runtime state required for allocation")
            state=read_model(state_path,SessionInputRuntimeState)
            if state.generation!=record.admitted_generation:raise InputRuntimeConflictError("admission generation mismatch")
            session_sequence=state.accepted_through_session_sequence+1
            if record.admission_kind==AdmissionKind.START_CYCLE:
                if state.active_cycle_id not in {None,record.target_cycle_id} or state.cycle_status not in {CycleStatus.IDLE,CycleStatus.DONE,CycleStatus.ERROR,CycleStatus.CANCELLED}:raise InputRuntimeConflictError("session already has active cycle")
                cycle_sequence=0;active_accepted=0
                next_state=validated_copy(state,active_cycle_id=record.target_cycle_id,cycle_status=CycleStatus.RUNNING,accepted_through_session_sequence=session_sequence,active_cycle_accepted_through_sequence=0,active_cycle_applied_through_sequence=0,revision=state.revision+1,updated_at=record.admitted_at)
            else:
                if state.active_cycle_id!=record.target_cycle_id:raise InputRuntimeConflictError("target cycle is not active")
                cycle_sequence=state.active_cycle_accepted_through_sequence+1;active_accepted=cycle_sequence
                next_state=validated_copy(state,accepted_through_session_sequence=session_sequence,active_cycle_accepted_through_sequence=active_accepted,revision=state.revision+1,updated_at=record.admitted_at)
            allocated=validated_copy(record,session_sequence=session_sequence,cycle_sequence=cycle_sequence)
            try:by_id=await self._find_id(allocated.admission_id)
            except InputRuntimeNotFoundError:by_id=None
            if by_id is not None and by_id!=allocated:raise InputRuntimeConflictError("admission stable ID collision")
            self._ensure_cycle_authority(allocated.target_cycle_id,allocated.session_id)
            atomic_write_model(self.layout.admission(allocated.session_id,allocated.admission_id),allocated);self._index_record(allocated);atomic_write_model(state_path,next_state)
            return allocated
    async def _replace(self,admission_id:str,**updates:object)->InputAdmissionRecord:
        record=await self._find_id(admission_id)
        async with self.locks.hold(self.root,record.session_id):
            current=await self._find_id(admission_id);updated=validated_copy(current,**updates);atomic_write_model(self.layout.admission(current.session_id,current.admission_id),updated);return updated
    async def mark_applied(self,admission_id:str,*,applied_at:datetime)->InputAdmissionRecord:return await self._replace(admission_id,state=AdmissionState.APPLIED,applied_at=applied_at)
    async def cancel(self,admission_id:str,*,cancelled_at:datetime,reason_code:str)->InputAdmissionRecord:return await self._replace(admission_id,state=AdmissionState.CANCELLED,cancelled_at=cancelled_at,cancellation_reason_code=reason_code)
    async def list_for_session(self,session_id:str)->tuple[InputAdmissionRecord,...]:return tuple(sorted(list_models(self.layout.admissions(session_id),InputAdmissionRecord),key=lambda x:x.session_sequence))
    async def list_unapplied(self,session_id:str)->tuple[InputAdmissionRecord,...]:return tuple(x for x in await self.list_for_session(session_id) if x.state==AdmissionState.ADMITTED)
    async def cancel_generation(self,session_id:str,*,generation:int,cancelled_at:datetime,reason_code:str)->tuple[InputAdmissionRecord,...]:
        out=[]
        async with self.locks.hold(self.root,session_id):
            for stale in await self.list_for_session(session_id):
                current=read_model(self.layout.admission(session_id,stale.admission_id),InputAdmissionRecord)
                if current.admitted_generation==generation and current.state==AdmissionState.ADMITTED:
                    u=validated_copy(current,state=AdmissionState.CANCELLED,cancelled_at=cancelled_at,cancellation_reason_code=reason_code);atomic_write_model(self.layout.admission(session_id,current.admission_id),u);out.append(u)
        return tuple(out)

class FileSystemSessionControlRepository(_RepositoryBase):
    async def _all(self,s:str)->tuple[SessionControlCommand,...]:return tuple(sorted(list_models(self.layout.controls(s),SessionControlCommand),key=lambda x:x.sequence_number))
    def _index(self,r:SessionControlCommand)->None:self._write_pointer(self.layout.record_index("control",r.control_id),self._pointer("control",r.control_id,r.session_id,self.layout.control(r.session_id,r.control_id),r.target_cycle_id))
    async def _find(self,control_id:str)->SessionControlCommand:
        p=self._read_pointer(self.layout.record_index("control",control_id))
        if p and self._pointer_record_path(p).exists():return read_model(self._pointer_record_path(p),SessionControlCommand)
        d=self.layout.root/"sessions";rows=[read_model(x,SessionControlCommand) for x in sorted(d.glob("*/controls/*.json")) if read_model(x,SessionControlCommand).control_id==control_id] if d.exists() else []
        if not rows:raise InputRuntimeNotFoundError(control_id)
        if len(rows)>1:raise InputRuntimeConflictError("duplicate control stable ID")
        self._index(rows[0]);return rows[0]
    async def append(self,command:SessionControlCommand)->SessionControlCommand:
        async with self.locks.hold(self.root,command.session_id):
            existing=await self.get_by_idempotency_key(command.session_id,command.idempotency_key)
            if existing is not None:
                if not _same_control_relation(existing,command):raise InputRuntimeConflictError("control idempotency relation changed")
                return existing
            try:by_id=await self._find(command.control_id)
            except InputRuntimeNotFoundError:by_id=None
            if by_id is not None and by_id!=command:raise InputRuntimeConflictError("control stable ID collision")
            if any(x.sequence_number==command.sequence_number for x in await self._all(command.session_id)):raise InputRuntimeConflictError("duplicate control sequence")
            atomic_write_model(self.layout.control(command.session_id,command.control_id),command);self._index(command);return command
    async def get_by_idempotency_key(self,session_id:str,idempotency_key:str)->SessionControlCommand|None:return next((x for x in await self._all(session_id) if x.idempotency_key==idempotency_key.strip()),None)
    async def _replace(self,control_id:str,**updates:object)->SessionControlCommand:
        stale=await self._find(control_id)
        async with self.locks.hold(self.root,stale.session_id):
            current=await self._find(control_id);u=validated_copy(current,**updates);atomic_write_model(self.layout.control(current.session_id,current.control_id),u);return u
    async def acknowledge(self,control_id:str,*,acknowledged_at:datetime)->SessionControlCommand:return await self._replace(control_id,state=ControlState.ACKNOWLEDGED,acknowledged_at=acknowledged_at)
    async def apply(self,control_id:str,*,applied_at:datetime)->SessionControlCommand:
        r=await self._find(control_id);return await self._replace(control_id,state=ControlState.APPLIED,acknowledged_at=r.acknowledged_at or applied_at,applied_at=applied_at)
    async def reject(self,control_id:str,*,rejection_code:str)->SessionControlCommand:return await self._replace(control_id,state=ControlState.REJECTED,rejection_code=rejection_code)
    async def list_pending(self,session_id:str,*,generation:int)->tuple[SessionControlCommand,...]:return tuple(x for x in await self._all(session_id) if x.generation==generation and x.state in {ControlState.QUEUED,ControlState.ACKNOWLEDGED})
    async def cancel_generation(self,session_id:str,*,generation:int,reason_code:str)->tuple[SessionControlCommand,...]:
        out=[]
        async with self.locks.hold(self.root,session_id):
            for stale in await self._all(session_id):
                current=read_model(self.layout.control(session_id,stale.control_id),SessionControlCommand)
                if current.generation==generation and current.state in {ControlState.QUEUED,ControlState.ACKNOWLEDGED}:
                    u=validated_copy(current,state=ControlState.CANCELLED,acknowledged_at=None,cancellation_reason_code=reason_code);atomic_write_model(self.layout.control(session_id,current.control_id),u);out.append(u)
        return tuple(out)
