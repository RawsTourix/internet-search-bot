"""Cycle inbox, snapshot, and context revision filesystem repositories."""
from __future__ import annotations
from datetime import datetime,timedelta,timezone
from uuid import uuid4
from .errors import InputRuntimeConflictError,InputRuntimeNotFoundError
from .models import ActiveCycleSnapshot,ClaimedInboxRange,CycleContextRevision,CycleInboxItem,CycleStatus,InboxState
from .serialization import atomic_write_model,list_models,read_model
from ._filesystem_common import _RepositoryBase,validated_copy

def _same_inbox_relation(a:CycleInboxItem,b:CycleInboxItem)->bool:
    return (a.admission_id,a.session_id,a.cycle_id,a.input_batch_id,a.cycle_sequence,a.generation,a.payload_size_bytes)==(b.admission_id,b.session_id,b.cycle_id,b.input_batch_id,b.cycle_sequence,b.generation,b.payload_size_bytes)

class FileSystemCycleInboxRepository(_RepositoryBase):
    def _index(self,r:CycleInboxItem)->None:
        p=self._pointer("inbox",r.inbox_item_id,r.session_id,self.layout.inbox_item(r.cycle_id,r.inbox_item_id),r.cycle_id)
        self._write_pointer(self.layout.record_index("inbox",r.inbox_item_id),p);self._write_pointer(self.layout.inbox_admission(r.admission_id),p);self._write_pointer(self.layout.inbox_input(r.input_batch_id),p);self._ensure_cycle_authority(r.cycle_id,r.session_id)
    def _from(self,path)->CycleInboxItem|None:
        p=self._read_pointer(path)
        if p and self._pointer_record_path(p).exists():return read_model(self._pointer_record_path(p),CycleInboxItem)
        return None
    def _scan(self)->tuple[CycleInboxItem,...]:
        d=self.layout.root/"cycles";return tuple(read_model(p,CycleInboxItem) for p in sorted(d.glob("*/inbox/*.json"))) if d.exists() else ()
    async def _find_id(self,rid:str)->CycleInboxItem:
        r=self._from(self.layout.record_index("inbox",rid))
        if r:return r
        rows=[x for x in self._scan() if x.inbox_item_id==rid]
        if not rows:raise InputRuntimeNotFoundError(rid)
        if len(rows)>1:raise InputRuntimeConflictError("duplicate inbox stable ID")
        self._index(rows[0]);return rows[0]
    async def create_if_absent(self,item:CycleInboxItem)->CycleInboxItem:
        async with self.locks.hold(self.root,item.session_id):
            for idx in (self.layout.inbox_admission(item.admission_id),self.layout.inbox_input(item.input_batch_id)):
                existing=self._from(idx)
                if existing is not None:
                    if not _same_inbox_relation(existing,item):raise InputRuntimeConflictError("inbox idempotency relation changed")
                    return existing
            try:by_id=await self._find_id(item.inbox_item_id)
            except InputRuntimeNotFoundError:by_id=None
            if by_id is not None:
                if by_id!=item:raise InputRuntimeConflictError("inbox stable ID collision")
                return by_id
            rows=await self.list_for_cycle(item.cycle_id)
            if any(x.cycle_sequence==item.cycle_sequence for x in rows):raise InputRuntimeConflictError("duplicate inbox cycle sequence")
            self._ensure_cycle_authority(item.cycle_id,item.session_id);atomic_write_model(self.layout.inbox_item(item.cycle_id,item.inbox_item_id),item);self._index(item);return item
    async def list_for_cycle(self,cycle_id:str)->tuple[CycleInboxItem,...]:return tuple(sorted(list_models(self.layout.inbox(cycle_id),CycleInboxItem),key=lambda x:x.cycle_sequence))
    async def claim_contiguous_range(self,cycle_id:str,*,generation:int,after_sequence:int,max_items:int,max_bytes:int,lease_seconds:int)->ClaimedInboxRange|None:
        auth=self._read_pointer(self.layout.cycle_authority(cycle_id))
        if auth is None:
            rows=await self.list_for_cycle(cycle_id)
            if not rows:return None
            self._ensure_cycle_authority(cycle_id,rows[0].session_id);session_id=rows[0].session_id
        else:session_id=auth.session_id
        async with self.locks.hold(self.root,session_id):
            rows=[x for x in await self.list_for_cycle(cycle_id) if x.generation==generation and x.cycle_sequence>after_sequence and x.state==InboxState.QUEUED]
            rows.sort(key=lambda x:x.cycle_sequence)
            if not rows or rows[0].cycle_sequence!=after_sequence+1:return None
            selected=[];total=0;expected=after_sequence+1
            for item in rows:
                if item.cycle_sequence!=expected or len(selected)>=max_items:break
                size=item.payload_size_bytes
                if size>max_bytes and not selected:return None
                if selected and total+size>max_bytes:break
                selected.append(item);total+=size;expected+=1
            if not selected:return None
            now=datetime.now(timezone.utc);expires=now+timedelta(seconds=lease_seconds);token=uuid4().hex;claimed=[]
            for stale in selected:
                current=read_model(self.layout.inbox_item(cycle_id,stale.inbox_item_id),CycleInboxItem)
                if current.state!=InboxState.QUEUED:raise InputRuntimeConflictError("inbox changed before claim")
                u=validated_copy(current,state=InboxState.CLAIMED,claim_token=token,claimed_at=now,claim_expires_at=expires,attempt_count=current.attempt_count+1);atomic_write_model(self.layout.inbox_item(cycle_id,current.inbox_item_id),u);claimed.append(u)
            return ClaimedInboxRange(cycle_id=cycle_id,generation=generation,claim_token=token,first_cycle_sequence=claimed[0].cycle_sequence,last_cycle_sequence=claimed[-1].cycle_sequence,items=tuple(claimed),claimed_bytes=total,claim_expires_at=expires)
    async def _transition(self,claim:ClaimedInboxRange,required:set[InboxState],next_state:InboxState,*,applied_at:datetime|None=None,error_code:str|None=None)->tuple[CycleInboxItem,...]:
        async with self.locks.hold(self.root,claim.items[0].session_id):
            currents=[]
            for old in claim.items:
                cur=read_model(self.layout.inbox_item(claim.cycle_id,old.inbox_item_id),CycleInboxItem)
                if cur.state not in required or cur.claim_token!=claim.claim_token or cur.generation!=claim.generation:raise InputRuntimeConflictError("stale inbox claim")
                currents.append(cur)
            out=[]
            for cur in currents:
                if next_state==InboxState.APPLIED:u=validated_copy(cur,state=next_state,applied_at=applied_at,claim_token=None,claimed_at=None,claim_expires_at=None,last_error_code=None)
                elif next_state==InboxState.QUEUED:u=validated_copy(cur,state=next_state,claim_token=None,claimed_at=None,claim_expires_at=None,last_error_code=error_code)
                else:u=validated_copy(cur,state=next_state)
                atomic_write_model(self.layout.inbox_item(cur.cycle_id,cur.inbox_item_id),u);out.append(u)
            return tuple(out)
    async def mark_applying(self,claim:ClaimedInboxRange)->ClaimedInboxRange:return validated_copy(claim,items=await self._transition(claim,{InboxState.CLAIMED},InboxState.APPLYING))
    async def mark_applied(self,claim:ClaimedInboxRange,*,applied_at:datetime)->tuple[CycleInboxItem,...]:return await self._transition(claim,{InboxState.CLAIMED,InboxState.APPLYING},InboxState.APPLIED,applied_at=applied_at)
    async def requeue_claim(self,claim:ClaimedInboxRange,*,error_code:str|None=None)->tuple[CycleInboxItem,...]:return await self._transition(claim,{InboxState.CLAIMED,InboxState.APPLYING},InboxState.QUEUED,error_code=error_code)
    async def recover_expired_claims(self,*,now:datetime)->tuple[CycleInboxItem,...]:
        out=[]
        for stale in self._scan():
            if stale.state not in {InboxState.CLAIMED,InboxState.APPLYING} or stale.claim_expires_at is None or stale.claim_expires_at>now:continue
            async with self.locks.hold(self.root,stale.session_id):
                cur=read_model(self.layout.inbox_item(stale.cycle_id,stale.inbox_item_id),CycleInboxItem)
                if cur.state not in {InboxState.CLAIMED,InboxState.APPLYING} or cur.claim_expires_at is None or cur.claim_expires_at>now:continue
                applied=False
                p=self.layout.snapshot(cur.cycle_id)
                if cur.state==InboxState.APPLYING and p.exists():
                    snap=read_model(p,ActiveCycleSnapshot);applied=snap.generation==cur.generation and snap.applied_through_cycle_sequence>=cur.cycle_sequence and cur.input_batch_id in snap.applied_input_batch_ids
                if applied:u=validated_copy(cur,state=InboxState.APPLIED,applied_at=now,claim_token=None,claimed_at=None,claim_expires_at=None,last_error_code=None)
                else:u=validated_copy(cur,state=InboxState.QUEUED,claim_token=None,claimed_at=None,claim_expires_at=None,last_error_code="claim_expired")
                atomic_write_model(self.layout.inbox_item(cur.cycle_id,cur.inbox_item_id),u);out.append(u)
        return tuple(out)
    async def cancel_generation(self,session_id:str,*,generation:int,cancelled_at:datetime,reason_code:str)->tuple[CycleInboxItem,...]:
        out=[]
        async with self.locks.hold(self.root,session_id):
            for stale in self._scan():
                if stale.session_id!=session_id:continue
                cur=read_model(self.layout.inbox_item(stale.cycle_id,stale.inbox_item_id),CycleInboxItem)
                if cur.generation==generation and cur.state in {InboxState.QUEUED,InboxState.CLAIMED,InboxState.APPLYING}:
                    u=validated_copy(cur,state=InboxState.CANCELLED,cancelled_at=cancelled_at,claim_token=None,claimed_at=None,claim_expires_at=None,last_error_code=reason_code);atomic_write_model(self.layout.inbox_item(cur.cycle_id,cur.inbox_item_id),u);out.append(u)
        return tuple(out)

class FileSystemActiveCycleSnapshotRepository(_RepositoryBase):
    def _index(self,r:ActiveCycleSnapshot)->None:self._write_pointer(self.layout.record_index("snapshot",r.cycle_id),self._pointer("snapshot",r.cycle_id,r.session_id,self.layout.snapshot(r.cycle_id),r.cycle_id));self._ensure_cycle_authority(r.cycle_id,r.session_id)
    async def create_if_absent(self,snapshot:ActiveCycleSnapshot)->ActiveCycleSnapshot:
        async with self.locks.hold(self.root,snapshot.session_id):
            p=self.layout.snapshot(snapshot.cycle_id)
            if p.exists():
                cur=read_model(p,ActiveCycleSnapshot)
                if cur!=snapshot:raise InputRuntimeConflictError("snapshot stable ID collision")
                return cur
            self._ensure_cycle_authority(snapshot.cycle_id,snapshot.session_id);atomic_write_model(p,snapshot);self._index(snapshot);return snapshot
    async def get(self,cycle_id:str)->ActiveCycleSnapshot|None:
        p=self.layout.snapshot(cycle_id);return read_model(p,ActiveCycleSnapshot) if p.exists() else None
    async def compare_and_swap(self,expected_revision:int,snapshot:ActiveCycleSnapshot)->ActiveCycleSnapshot:
        async with self.locks.hold(self.root,snapshot.session_id):
            p=self.layout.snapshot(snapshot.cycle_id)
            if not p.exists():raise InputRuntimeNotFoundError(snapshot.cycle_id)
            cur=read_model(p,ActiveCycleSnapshot)
            if cur.snapshot_revision!=expected_revision or snapshot.snapshot_revision!=expected_revision+1:raise InputRuntimeConflictError("stale snapshot revision")
            if (cur.session_id,cur.cycle_id,cur.generation)!=(snapshot.session_id,snapshot.cycle_id,snapshot.generation):raise InputRuntimeConflictError("snapshot identity changed")
            atomic_write_model(p,snapshot);return snapshot
    async def _all(self)->tuple[ActiveCycleSnapshot,...]:
        d=self.layout.root/"cycles";return tuple(read_model(p,ActiveCycleSnapshot) for p in sorted(d.glob("*/snapshot.json"))) if d.exists() else ()
    async def list_active(self)->tuple[ActiveCycleSnapshot,...]:return tuple(x for x in await self._all() if x.status not in {CycleStatus.DONE,CycleStatus.ERROR,CycleStatus.CANCELLED})
    async def list_resumable(self)->tuple[ActiveCycleSnapshot,...]:return tuple(x for x in await self._all() if x.status in {CycleStatus.RUNNING,CycleStatus.WAITING_USER,CycleStatus.PAUSE_REQUESTED,CycleStatus.PAUSED_BY_USER,CycleStatus.INTERRUPTED,CycleStatus.FINALIZING})
    async def cancel_generation(self,session_id:str,*,generation:int,reason_code:str)->tuple[ActiveCycleSnapshot,...]:
        out=[]
        async with self.locks.hold(self.root,session_id):
            for stale in await self._all():
                if stale.session_id!=session_id:continue
                p=self.layout.snapshot(stale.cycle_id);cur=read_model(p,ActiveCycleSnapshot)
                if cur.generation==generation and cur.status in {CycleStatus.RUNNING,CycleStatus.WAITING_USER,CycleStatus.PAUSE_REQUESTED,CycleStatus.PAUSED_BY_USER,CycleStatus.INTERRUPTED,CycleStatus.FINALIZING}:
                    u=validated_copy(cur,status=CycleStatus.CANCELLED,cancellation_reason_code=reason_code,waiting_question=None,pause_reason=None,interruption_reason=None,snapshot_revision=cur.snapshot_revision+1,updated_at=datetime.now(timezone.utc));atomic_write_model(p,u);out.append(u)
        return tuple(out)

class FileSystemContextRevisionRepository(_RepositoryBase):
    def _index(self,r:CycleContextRevision)->None:self._write_pointer(self.layout.record_index("revision",r.context_revision_id),self._pointer("revision",r.context_revision_id,r.session_id,self.layout.revision(r.cycle_id,r.context_revision_id),r.cycle_id));self._ensure_cycle_authority(r.cycle_id,r.session_id)
    async def append_revision(self,r:CycleContextRevision)->CycleContextRevision:
        async with self.locks.hold(self.root,r.session_id):
            p=self.layout.revision(r.cycle_id,r.context_revision_id)
            if p.exists():
                cur=read_model(p,CycleContextRevision)
                if cur!=r:raise InputRuntimeConflictError("context revision stable ID collision")
                return cur
            latest=await self.get_latest(r.cycle_id)
            if latest is None and r.revision_number!=1:raise InputRuntimeConflictError("first revision must be 1")
            if latest is not None and (r.revision_number!=latest.revision_number+1 or r.parent_revision_ids!=[latest.context_revision_id]):raise InputRuntimeConflictError("context revision sequence/parent mismatch")
            self._ensure_cycle_authority(r.cycle_id,r.session_id);atomic_write_model(p,r);self._index(r);return r
    async def get(self,context_revision_id:str)->CycleContextRevision|None:
        ptr=self._read_pointer(self.layout.record_index("revision",context_revision_id))
        if ptr and self._pointer_record_path(ptr).exists():return read_model(self._pointer_record_path(ptr),CycleContextRevision)
        d=self.layout.root/"cycles";rows=[read_model(p,CycleContextRevision) for p in sorted(d.glob("*/context-revisions/*.json")) if read_model(p,CycleContextRevision).context_revision_id==context_revision_id] if d.exists() else []
        if len(rows)>1:raise InputRuntimeConflictError("duplicate context revision ID")
        if rows:self._index(rows[0]);return rows[0]
        return None
    async def get_latest(self,cycle_id:str)->CycleContextRevision|None:
        rows=await self.list_for_cycle(cycle_id);return rows[-1] if rows else None
    async def list_for_cycle(self,cycle_id:str)->tuple[CycleContextRevision,...]:return tuple(sorted(list_models(self.layout.revisions(cycle_id),CycleContextRevision),key=lambda x:x.revision_number))
