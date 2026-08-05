"""Shared filesystem repository primitives and recoverable indexes."""
from __future__ import annotations
from pathlib import Path
from typing import Iterable,TypeVar
from pydantic import BaseModel,ConfigDict
from .coordination import GLOBAL_SESSION_LOCKS,SessionLockRegistry
from .serialization import atomic_write_model,read_model,storage_key
ModelT=TypeVar("ModelT",bound=BaseModel)
def validated_copy(record:ModelT,**updates:object)->ModelT:
    data=record.model_dump(mode="json");data.update(updates);return type(record).model_validate(data)
class _IndexPointer(BaseModel):
    model_config=ConfigDict(extra="forbid")
    record_type:str;record_id:str;session_id:str;cycle_id:str|None=None;relative_path:str
class _Layout:
    def __init__(self,root:Path)->None:self.root=root/"input-runtime"
    def session_dir(self,s:str)->Path:return self.root/"sessions"/storage_key(s)
    def cycle_dir(self,c:str)->Path:return self.root/"cycles"/storage_key(c)
    def state(self,s:str)->Path:return self.session_dir(s)/"state.json"
    def admissions(self,s:str)->Path:return self.session_dir(s)/"admissions"
    def admission(self,s:str,r:str)->Path:return self.admissions(s)/f"{storage_key(r)}.json"
    def inbox(self,c:str)->Path:return self.cycle_dir(c)/"inbox"
    def inbox_item(self,c:str,r:str)->Path:return self.inbox(c)/f"{storage_key(r)}.json"
    def controls(self,s:str)->Path:return self.session_dir(s)/"controls"
    def control(self,s:str,r:str)->Path:return self.controls(s)/f"{storage_key(r)}.json"
    def snapshot(self,c:str)->Path:return self.cycle_dir(c)/"snapshot.json"
    def revisions(self,c:str)->Path:return self.cycle_dir(c)/"context-revisions"
    def revision(self,c:str,r:str)->Path:return self.revisions(c)/f"{storage_key(r)}.json"
    def emissions(self,c:str)->Path:return self.cycle_dir(c)/"emissions"
    def emission(self,c:str,r:str)->Path:return self.emissions(c)/f"{storage_key(r)}.json"
    def finalizations(self,c:str)->Path:return self.cycle_dir(c)/"finalizations"
    def finalization(self,c:str,r:str)->Path:return self.finalizations(c)/f"{storage_key(r)}.json"
    def index(self,kind:str,key:str)->Path:return self.root/"indexes"/kind/f"{storage_key(key)}.json"
    def cycle_authority(self,c:str)->Path:return self.index("cycle-authority",c)
    def record_index(self,t:str,r:str)->Path:return self.index(f"records-{t}",r)
    def admission_input(self,b:str)->Path:return self.index("admission-by-input",b)
    def inbox_admission(self,a:str)->Path:return self.index("inbox-by-admission",a)
    def inbox_input(self,b:str)->Path:return self.index("inbox-by-input",b)
class _RepositoryBase:
    def __init__(self,*,root:Path,locks:SessionLockRegistry=GLOBAL_SESSION_LOCKS)->None:self.root=root;self.layout=_Layout(root);self.locks=locks
    @staticmethod
    def _all_json(d:Path)->Iterable[Path]:return sorted(d.rglob("*.json")) if d.exists() else ()
    def _pointer(self,record_type:str,record_id:str,session_id:str,path:Path,cycle_id:str|None=None)->_IndexPointer:
        return _IndexPointer(record_type=record_type,record_id=record_id,session_id=session_id,cycle_id=cycle_id,relative_path=str(path.relative_to(self.layout.root)))
    def _write_pointer(self,path:Path,pointer:_IndexPointer)->None:atomic_write_model(path,pointer)
    def _read_pointer(self,path:Path)->_IndexPointer|None:
        if not path.exists():return None
        try:return read_model(path,_IndexPointer)
        except Exception:return None
    def _pointer_record_path(self,p:_IndexPointer)->Path:return self.layout.root/p.relative_path
    def _ensure_cycle_authority(self,cycle_id:str,session_id:str)->None:
        path=self.layout.cycle_authority(cycle_id);p=self._read_pointer(path)
        if p is not None and p.session_id!=session_id:raise ValueError("cycle belongs to another session")
        if p is None:self._write_pointer(path,self._pointer("cycle",cycle_id,session_id,self.layout.cycle_dir(cycle_id),cycle_id))
