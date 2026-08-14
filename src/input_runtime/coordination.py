"""Short in-process coordination for filesystem repositories."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

from .serialization import storage_key


@dataclass
class _Entry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    references: int = 0
    waiters: int = 0
    owners: int = 0


class SessionLockRegistry:
    """Bounded registry for root identity and session coordination locks.

    Repository lock ordering is always root identity first, then session.
    Admission services use a distinct application lock key, so they may hold
    one admission decision boundary while repositories acquire their normal
    root/session locks without re-entering the same asyncio lock.
    """

    def __init__(self, *, max_entries: int = 1024) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._max_entries = max_entries
        self._guard = asyncio.Lock()
        self._entries: OrderedDict[tuple[str, str], _Entry] = OrderedDict()

    def _root_key(self, root: Path) -> tuple[str, str]:
        return (str(root.resolve()), "root-identity")

    def _session_key(self, root: Path, session_id: str) -> tuple[str, str]:
        return (str(root.resolve()), f"session:{storage_key(session_id)}")

    def _admission_key(self, root: Path, session_id: str) -> tuple[str, str]:
        return (
            str(root.resolve()),
            f"admission-session:{storage_key(session_id)}",
        )

    def _validate_entry_locked(self, entry: _Entry) -> None:
        if min(entry.references, entry.waiters, entry.owners) < 0:
            raise RuntimeError("lock registry counters became negative")
        if entry.owners > 1:
            raise RuntimeError("session lock has more than one owner")
        if entry.owners and not entry.lock.locked():
            raise RuntimeError("owned registry entry must hold its lock")

    def _prune_locked(self) -> None:
        if len(self._entries) <= self._max_entries:
            return
        for key, entry in list(self._entries.items()):
            if len(self._entries) <= self._max_entries:
                break
            self._validate_entry_locked(entry)
            if (
                entry.references == 0
                and entry.waiters == 0
                and entry.owners == 0
                and not entry.lock.locked()
            ):
                self._entries.pop(key, None)

    async def cleanup(self) -> int:
        async with self._guard:
            before = len(self._entries)
            self._prune_locked()
            return before - len(self._entries)

    @property
    def size(self) -> int:
        return len(self._entries)

    async def _after_lock_acquired(
        self,
        key: tuple[str, str],
        entry: _Entry,
    ) -> None:
        """Test seam after lock acquisition but before registry bookkeeping."""

    async def _finish_hold(
        self,
        *,
        key: tuple[str, str],
        entry: _Entry,
        acquired: bool,
        owner_bookkept: bool,
    ) -> None:
        async with self._guard:
            if acquired:
                if owner_bookkept:
                    entry.owners -= 1
                else:
                    entry.waiters -= 1
                entry.lock.release()
            else:
                entry.waiters -= 1
            entry.references -= 1
            self._validate_entry_locked(entry)
            if key in self._entries:
                self._entries.move_to_end(key, last=True)
            self._prune_locked()

    @asynccontextmanager
    async def _hold_key(self, key: tuple[str, str]) -> AsyncIterator[None]:
        async with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                entry = _Entry()
                self._entries[key] = entry
            else:
                self._entries.move_to_end(key)
            entry.references += 1
            entry.waiters += 1
            self._validate_entry_locked(entry)
            self._prune_locked()

        acquired = False
        owner_bookkept = False
        try:
            await entry.lock.acquire()
            acquired = True
            await self._after_lock_acquired(key, entry)
            async with self._guard:
                entry.waiters -= 1
                entry.owners += 1
                owner_bookkept = True
                self._validate_entry_locked(entry)
            yield
        finally:
            cleanup_task = asyncio.create_task(
                self._finish_hold(
                    key=key,
                    entry=entry,
                    acquired=acquired,
                    owner_bookkept=owner_bookkept,
                )
            )
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await cleanup_task

    @asynccontextmanager
    async def hold(self, root: Path, session_id: str) -> AsyncIterator[None]:
        async with self._hold_key(self._session_key(root, session_id)):
            yield

    @asynccontextmanager
    async def hold_admission(
        self,
        root: Path,
        session_id: str,
    ) -> AsyncIterator[None]:
        """Serialize one complete capacity/allocate/inbox admission boundary."""
        async with self._hold_key(self._admission_key(root, session_id)):
            yield

    @asynccontextmanager
    async def hold_root(self, root: Path) -> AsyncIterator[None]:
        async with self._hold_key(self._root_key(root)):
            yield

    @asynccontextmanager
    async def hold_identity_then_session(
        self,
        root: Path,
        session_id: str,
    ) -> AsyncIterator[None]:
        async with self.hold_root(root):
            async with self.hold(root, session_id):
                yield


GLOBAL_SESSION_LOCKS = SessionLockRegistry()
