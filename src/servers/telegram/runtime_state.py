"""Small in-process coordination primitives for Telegram transport workflows."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import AsyncIterator


@dataclass(slots=True)
class _LockEntry:
    lock: asyncio.Lock
    users: int = 0


class KeyedAsyncLockPool:
    """Serialize one idempotency key and remove entries after the last waiter."""

    def __init__(self) -> None:
        self._entries: dict[str, _LockEntry] = {}
        self._guard = asyncio.Lock()

    @asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        normalized = key.strip()
        if not normalized:
            raise ValueError("lock key must not be empty")
        async with self._guard:
            entry = self._entries.get(normalized)
            if entry is None:
                entry = _LockEntry(lock=asyncio.Lock())
                self._entries[normalized] = entry
            entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            async with self._guard:
                current = self._entries.get(normalized)
                if current is not entry:
                    return
                entry.users -= 1
                if entry.users <= 0 and not entry.lock.locked():
                    self._entries.pop(normalized, None)

    async def size(self) -> int:
        async with self._guard:
            return len(self._entries)
