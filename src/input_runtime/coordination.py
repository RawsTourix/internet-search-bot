"""Short in-process coordination for filesystem repositories."""
from __future__ import annotations
import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass,field
from pathlib import Path
from typing import AsyncIterator
from .serialization import storage_key

@dataclass
class _Entry:
    lock:asyncio.Lock=field(default_factory=asyncio.Lock)
    references:int=0
    waiters:int=0
    owners:int=0

class SessionLockRegistry:
    """Bounded registry that never evicts referenced, waiting, or owned locks."""
    def __init__(self,*,max_entries:int=1024)->None:
        if max_entries<=0:raise ValueError("max_entries must be positive")
        self._max_entries=max_entries;self._guard=asyncio.Lock();self._entries:OrderedDict[tuple[str,str],_Entry]=OrderedDict()
    def _key(self,root:Path,session_id:str)->tuple[str,str]:return(str(root.resolve()),storage_key(session_id))
    def _prune_locked(self)->None:
        if len(self._entries)<=self._max_entries:return
        for key,entry in list(self._entries.items()):
            if len(self._entries)<=self._max_entries:break
            if entry.references==0 and entry.waiters==0 and entry.owners==0 and not entry.lock.locked():self._entries.pop(key,None)
    async def cleanup(self)->int:
        async with self._guard:
            before=len(self._entries);self._prune_locked();return before-len(self._entries)
    @property
    def size(self)->int:return len(self._entries)
    @asynccontextmanager
    async def hold(self,root:Path,session_id:str)->AsyncIterator[None]:
        key=self._key(root,session_id)
        async with self._guard:
            entry=self._entries.get(key)
            if entry is None:
                entry=_Entry();self._entries[key]=entry
            else:self._entries.move_to_end(key)
            entry.references+=1;entry.waiters+=1;self._prune_locked()
        acquired=False
        try:
            await entry.lock.acquire();acquired=True
            async with self._guard:
                entry.waiters-=1;entry.owners+=1
            yield
        finally:
            if acquired:
                entry.lock.release()
                async with self._guard:
                    entry.owners-=1;entry.references-=1;self._entries.move_to_end(key,last=True);self._prune_locked()
            else:
                async with self._guard:
                    entry.waiters-=1;entry.references-=1;self._prune_locked()
GLOBAL_SESSION_LOCKS=SessionLockRegistry()
