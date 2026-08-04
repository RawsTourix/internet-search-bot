"""Small in-process coordination primitives for Telegram transport workflows."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable


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


class TelegramSessionDispatcher:
    """One explicit FIFO lane per Telegram conversation/thread.

    ``submit`` is intentionally synchronous: the operation is appended at the
    exact moment ``Application.process_update(update)`` is called, before
    python-telegram-bot creates a background task.  This is the distinction
    from a pool of ``asyncio.Lock`` objects: locks provide mutual exclusion but
    do not preserve admission order when several already-created tasks race to
    acquire them.
    """

    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue[_QueuedOperation]] = {}
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._closing = False
        self._generations: dict[str, int] = {}

    @staticmethod
    def _normalize_key(key: str) -> str:
        normalized = key.strip()
        if not normalized:
            raise ValueError("dispatcher key must not be empty")
        return normalized

    def submit(self, key: str, operation: Operation) -> Awaitable[Any]:
        """Append one operation now and return an awaitable for its result."""

        normalized = self._normalize_key(key)
        if self._closing:
            raise RuntimeError("Telegram session dispatcher is shutting down")

        # A callback that is already executing inside its own lane must not
        # enqueue behind itself and deadlock.  Execute it inline instead.
        if _CURRENT_DISPATCH_KEY.get() == normalized:
            return operation()

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        queue = self._queues.get(normalized)
        if queue is None:
            queue = asyncio.Queue()
            self._queues[normalized] = queue
        queue.put_nowait(_QueuedOperation(
            operation=operation,
            future=future,
            generation=self._generations.get(normalized, 0),
        ))

        worker = self._workers.get(normalized)
        if worker is None or worker.done():
            self._workers[normalized] = loop.create_task(
                self._run_lane(normalized, queue),
                name=f"telegram-session-dispatch:{normalized}",
            )

        async def wait_for_result() -> Any:
            return await future

        return wait_for_result()

    async def run(self, key: str, operation: Operation) -> Any:
        """Append an internal callback and wait until its FIFO turn completes."""

        return await self.submit(key, operation)

    async def _run_lane(
        self,
        key: str,
        queue: asyncio.Queue[_QueuedOperation],
    ) -> None:
        token = _CURRENT_DISPATCH_KEY.set(key)
        try:
            while True:
                item = await queue.get()
                try:
                    if item.generation != self._generations.get(key, 0):
                        raise RuntimeError(
                            "Telegram update was invalidated by session reset"
                        )
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
                    queue.task_done()

                # No await between the emptiness check and registry cleanup:
                # synchronous submit() either happened before this block (and
                # the queue is non-empty) or will create a fresh worker after it.
                if queue.empty():
                    self._queues.pop(key, None)
                    self._workers.pop(key, None)
                    return
        finally:
            _CURRENT_DISPATCH_KEY.reset(token)

    async def reset_session(self, key: str) -> int:
        """Invalidate queued callbacks for one Telegram session generation."""

        normalized = self._normalize_key(key)
        generation = self._generations.get(normalized, 0) + 1
        self._generations[normalized] = generation
        queue = self._queues.get(normalized)
        if queue is not None:
            while not queue.empty():
                item = queue.get_nowait()
                queue.task_done()
                if not item.future.done():
                    item.future.set_exception(
                        RuntimeError(
                            "Telegram update was invalidated by session reset"
                        )
                    )
        return generation

    async def shutdown(self) -> None:
        """Cancel lanes and settle every queued waiter during application stop."""

        self._closing = True
        workers = list(self._workers.values())
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)

        for queue in self._queues.values():
            while not queue.empty():
                item = queue.get_nowait()
                if not item.future.done():
                    item.future.cancel()
                queue.task_done()
        self._queues.clear()
        self._workers.clear()

    def lane_count(self) -> int:
        return len(self._workers)


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
