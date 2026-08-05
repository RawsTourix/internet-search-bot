"""Short in-process coordination for filesystem repositories."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from .serialization import storage_key


class SessionLockRegistry:
    """Bounded process-wide lock registry keyed by root and normalized session."""

    def __init__(self, *, max_entries: int = 1024) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._max_entries = max_entries
        self._guard = asyncio.Lock()
        self._locks: OrderedDict[tuple[str, str], asyncio.Lock] = OrderedDict()

    async def _get_lock(self, root: Path, session_id: str) -> asyncio.Lock:
        key = (str(root.resolve()), storage_key(session_id))
        async with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[key] = lock
            else:
                self._locks.move_to_end(key)
            self._prune_unlocked()
            return lock

    def _prune_unlocked(self) -> None:
        if len(self._locks) <= self._max_entries:
            return
        for key, lock in list(self._locks.items()):
            if len(self._locks) <= self._max_entries:
                break
            if not lock.locked():
                self._locks.pop(key, None)

    async def cleanup(self) -> int:
        async with self._guard:
            before = len(self._locks)
            self._prune_unlocked()
            return before - len(self._locks)

    @property
    def size(self) -> int:
        return len(self._locks)

    @asynccontextmanager
    async def hold(self, root: Path, session_id: str) -> AsyncIterator[None]:
        lock = await self._get_lock(root, session_id)
        async with lock:
            yield


GLOBAL_SESSION_LOCKS = SessionLockRegistry()
