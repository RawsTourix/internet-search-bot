"""Small in-process coordination primitives for Telegram transport workflows."""

from __future__ import annotations

import asyncio
import sys
from collections import deque
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Deque


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


Operation = Callable[[], Awaitable[Any]]
_CURRENT_DISPATCH_KEY: ContextVar[str | None] = ContextVar(
    "telegram_current_dispatch_key",
    default=None,
)


@dataclass(slots=True)
class _QueuedOperation:
    operation: Operation
    future: asyncio.Future[Any]
    generation: int
    shared: bool


@dataclass(slots=True)
class _SessionLane:
    pending: Deque[_QueuedOperation]
    tasks: set[asyncio.Task[None]]
    active_shared: int = 0
    active_exclusive: bool = False


class TelegramSessionDispatcher:
    """A fair shared/exclusive admission lane per Telegram conversation.

    Exclusive operations preserve exact FIFO barriers for collection commands.
    Consecutive shared operations execute concurrently, while an exclusive
    operation waits for every earlier shared operation and prevents later work
    from overtaking it. Admission is intentionally synchronous: updates enter
    the lane before python-telegram-bot creates background handler tasks.
    """

    def __init__(self) -> None:
        self._lanes: dict[str, _SessionLane] = {}
        self._closing = False
        self._generations: dict[str, int] = {}

    @staticmethod
    def _normalize_key(key: str) -> str:
        normalized = key.strip()
        if not normalized:
            raise ValueError("dispatcher key must not be empty")
        return normalized

    def submit(self, key: str, operation: Operation) -> Awaitable[Any]:
        """Append one exclusive operation and return its result awaitable."""

        return self._submit(key, operation, shared=False)

    def submit_shared(self, key: str, operation: Operation) -> Awaitable[Any]:
        """Append concurrent work without crossing an exclusive FIFO barrier."""

        return self._submit(key, operation, shared=True)

    def _submit(
        self,
        key: str,
        operation: Operation,
        *,
        shared: bool,
    ) -> Awaitable[Any]:
        """Synchronously admit one operation and return a coroutine waiter."""

        normalized = self._normalize_key(key)
        if self._closing:
            raise RuntimeError("Telegram session dispatcher is shutting down")

        # A callback that is already executing inside its own lane must not
        # enqueue behind itself and deadlock. Execute it inline instead.
        if _CURRENT_DISPATCH_KEY.get() == normalized:
            return operation()

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        lane = self._lanes.get(normalized)
        if lane is None:
            lane = _SessionLane(pending=deque(), tasks=set())
            self._lanes[normalized] = lane
        lane.pending.append(_QueuedOperation(
            operation=operation,
            future=future,
            generation=self._generations.get(normalized, 0),
            shared=shared,
        ))
        self._schedule_lane(normalized, lane, loop=loop)

        async def wait_for_result() -> Any:
            return await future

        return wait_for_result()

    async def run(self, key: str, operation: Operation) -> Any:
        """Append an internal callback and wait until its FIFO turn completes."""

        return await self.submit(key, operation)

    def _schedule_lane(
        self,
        key: str,
        lane: _SessionLane,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        if self._closing or self._lanes.get(key) is not lane:
            return
        if lane.active_exclusive:
            return

        event_loop = loop or asyncio.get_running_loop()
        if lane.active_shared:
            # Admit newly arriving shared work immediately, but never cross an
            # exclusive item already waiting at the head of the FIFO queue.
            while lane.pending and lane.pending[0].shared:
                self._start_item(key, lane, lane.pending.popleft(), event_loop)
            return

        if not lane.pending:
            self._lanes.pop(key, None)
            return

        if not lane.pending[0].shared:
            self._start_item(key, lane, lane.pending.popleft(), event_loop)
            return

        # One shared phase consists of every consecutive data update admitted
        # before the next command barrier.
        while lane.pending and lane.pending[0].shared:
            self._start_item(key, lane, lane.pending.popleft(), event_loop)

    def _start_item(
        self,
        key: str,
        lane: _SessionLane,
        item: _QueuedOperation,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        if item.shared:
            lane.active_shared += 1
        else:
            lane.active_exclusive = True
        task = loop.create_task(
            self._execute_item(key, lane, item),
            name=f"telegram-session-dispatch:{key}",
        )
        lane.tasks.add(task)

    async def _execute_item(
        self,
        key: str,
        lane: _SessionLane,
        item: _QueuedOperation,
    ) -> None:
        token = _CURRENT_DISPATCH_KEY.set(key)
        try:
            if item.generation != self._generations.get(key, 0):
                raise RuntimeError("Telegram update was invalidated by session reset")
            result = await item.operation()
        except asyncio.CancelledError:
            if not item.future.done():
                item.future.cancel()
            raise
        except BaseException as error:
            if not item.future.done():
                item.future.set_exception(error)
        else:
            if not item.future.done():
                item.future.set_result(result)
        finally:
            _CURRENT_DISPATCH_KEY.reset(token)
            task = asyncio.current_task()
            if task is not None:
                lane.tasks.discard(task)
            if item.shared:
                lane.active_shared -= 1
            else:
                lane.active_exclusive = False
            self._schedule_lane(key, lane)

    async def reset_session(self, key: str) -> int:
        """Invalidate queued callbacks for one Telegram session generation."""

        normalized = self._normalize_key(key)
        generation = self._generations.get(normalized, 0) + 1
        self._generations[normalized] = generation
        lane = self._lanes.get(normalized)
        if lane is not None:
            while lane.pending:
                item = lane.pending.popleft()
                if not item.future.done():
                    item.future.set_exception(
                        RuntimeError(
                            "Telegram update was invalidated by session reset"
                        )
                    )
            self._schedule_lane(normalized, lane)
        return generation

    async def shutdown(self) -> None:
        """Cancel lanes and settle every queued waiter during application stop."""

        self._closing = True
        tasks = [task for lane in self._lanes.values() for task in lane.tasks]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        for lane in self._lanes.values():
            while lane.pending:
                item = lane.pending.popleft()
                if not item.future.done():
                    item.future.cancel()
        self._lanes.clear()

    def lane_count(self) -> int:
        return len(self._lanes)

    def has_pending(self, key: str) -> bool:
        """Return whether the exact session already owns an admission lane.

        The check is synchronous on purpose. ``Application.process_update``
        calls it before creating a handler task, so a data update arriving
        immediately after ``/collect`` can be appended behind that command
        without racing an ``await`` boundary.
        """

        normalized = self._normalize_key(key)
        lane = self._lanes.get(normalized)
        return bool(
            lane is not None
            and (
                lane.pending
                or lane.active_shared
                or lane.active_exclusive
            )
        )


class SessionGenerationRegistry:
    """Cheap event-loop-local epoch fencing for callbacks and progress edits."""

    def __init__(self) -> None:
        self._values: dict[str, int] = {}

    def current(self, session_id: str) -> int:
        return self._values.get(session_id, 0)

    def advance(self, session_id: str) -> int:
        generation = self.current(session_id) + 1
        self._values[session_id] = generation
        return generation

    def is_current(self, session_id: str, generation: int) -> bool:
        return self.current(session_id) == generation


def _install_ir5_runtime_control_handlers_if_host_ready() -> None:
    """Use this transport composition seam without importing app authority.

    ``telegram_server`` creates its ``Application`` before importing this module,
    so the concrete transport can register high-priority runtime-control
    handlers here while the application/domain layers remain Telegram-neutral.
    The handler itself lazily resolves server helpers only when an update runs.
    """
    from .runtime_control_handlers import install_runtime_control_handlers

    for module_name, module in tuple(sys.modules.items()):
        if not module_name.endswith(".servers.telegram.telegram_server"):
            continue
        application = getattr(module, "application", None)
        if application is not None:
            install_runtime_control_handlers(application)
            return


_install_ir5_runtime_control_handlers_if_host_ready()
