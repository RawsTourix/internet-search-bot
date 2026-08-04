"""Strict in-process execution ordering for one agent session.

This is deliberately smaller than the future CycleInbox. It provides the
baseline guarantees needed by current transports: committed batches enter one
FIFO lane per session, while a second lease immediately around the agent
runtime protects callers which bypass that lane.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable


RunOperation = Callable[[], Awaitable[Any]]


class SessionExecutionReset(RuntimeError):
    """A queued run was invalidated by a session reset."""


@dataclass(frozen=True, slots=True)
class SessionExecutionSnapshot:
    session_id: str
    generation: int
    active_cycle_id: str | None
    active_input_batch_id: str | None
    runtime_status: str
    run_started_at_monotonic: float | None
    queued_batches: int
    stop_requested: bool

    @property
    def run_seconds(self) -> float | None:
        if self.run_started_at_monotonic is None:
            return None
        return max(0.0, time.monotonic() - self.run_started_at_monotonic)


@dataclass(slots=True)
class _QueuedRun:
    input_batch_id: str
    generation: int
    operation: RunOperation
    future: asyncio.Future[Any]


@dataclass(slots=True)
class _SessionLane:
    generation: int = 0
    queue: deque[_QueuedRun] = field(default_factory=deque)
    worker: asyncio.Task[None] | None = None
    run_lease: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_cycle_id: str | None = None
    active_input_batch_id: str | None = None
    run_started_at_monotonic: float | None = None
    runtime_status: str = "idle"
    stop_requested: bool = False


class SessionExecutionCoordinator:
    """Own a FIFO committed-batch lane and defensive run lease per session."""

    def __init__(self) -> None:
        self._lanes: dict[str, _SessionLane] = {}
        self._guard = asyncio.Lock()
        self._closing = False

    @staticmethod
    def _session_id(value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("session_id must not be empty")
        return normalized

    async def enqueue(
        self,
        *,
        session_id: str,
        input_batch_id: str,
        operation: RunOperation,
    ) -> Any:
        session_id = self._session_id(session_id)
        input_batch_id = input_batch_id.strip()
        if not input_batch_id:
            raise ValueError("input_batch_id must not be empty")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        async with self._guard:
            if self._closing:
                raise RuntimeError("session execution coordinator is shutting down")
            lane = self._lanes.setdefault(session_id, _SessionLane())
            lane.queue.append(
                _QueuedRun(
                    input_batch_id=input_batch_id,
                    generation=lane.generation,
                    operation=operation,
                    future=future,
                )
            )
            if lane.worker is None or lane.worker.done():
                lane.worker = loop.create_task(
                    self._run_lane(session_id, lane),
                    name=f"session-execution:{session_id}",
                )
        return await future

    async def _run_lane(self, session_id: str, lane: _SessionLane) -> None:
        while True:
            async with self._guard:
                if not lane.queue:
                    lane.worker = None
                    return
                item = lane.queue.popleft()
                if item.generation != lane.generation:
                    if not item.future.done():
                        item.future.set_exception(
                            SessionExecutionReset(
                                "queued batch was invalidated by session reset"
                            )
                        )
                    continue
                lane.active_input_batch_id = item.input_batch_id
                lane.run_started_at_monotonic = time.monotonic()
                lane.runtime_status = "running"
                lane.stop_requested = False
            try:
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
                async with self._guard:
                    if (
                        lane.active_input_batch_id == item.input_batch_id
                        and lane.active_cycle_id is None
                    ):
                        lane.active_cycle_id = None
                        lane.active_input_batch_id = None
                        lane.run_started_at_monotonic = None
                        lane.runtime_status = "idle"

    @asynccontextmanager
    async def run_lease(
        self,
        *,
        session_id: str,
        input_batch_id: str | None = None,
        cycle_id: str | None = None,
    ) -> AsyncIterator[None]:
        """Serialize the final call into the LLM/tool cycle runtime."""

        session_id = self._session_id(session_id)
        async with self._guard:
            lane = self._lanes.setdefault(session_id, _SessionLane())
            generation = lane.generation
        async with lane.run_lease:
            async with self._guard:
                if generation != lane.generation:
                    raise SessionExecutionReset(
                        "agent run was invalidated before lease acquisition"
                    )
                lane.active_input_batch_id = input_batch_id
                lane.run_started_at_monotonic = time.monotonic()
                lane.runtime_status = "running"
                lane.active_cycle_id = cycle_id
            try:
                yield
            finally:
                async with self._guard:
                    if lane.active_cycle_id == cycle_id:
                        lane.active_cycle_id = None
                        lane.active_input_batch_id = None
                        lane.run_started_at_monotonic = None
                        lane.runtime_status = "idle"

    async def snapshot(self, session_id: str) -> SessionExecutionSnapshot:
        session_id = self._session_id(session_id)
        async with self._guard:
            lane = self._lanes.get(session_id)
            if lane is None:
                return SessionExecutionSnapshot(
                    session_id=session_id,
                    generation=0,
                    active_cycle_id=None,
                    active_input_batch_id=None,
                    runtime_status="idle",
                    run_started_at_monotonic=None,
                    queued_batches=0,
                    stop_requested=False,
                )
            return SessionExecutionSnapshot(
                session_id=session_id,
                generation=lane.generation,
                active_cycle_id=lane.active_cycle_id,
                active_input_batch_id=lane.active_input_batch_id,
                runtime_status=lane.runtime_status,
                run_started_at_monotonic=lane.run_started_at_monotonic,
                queued_batches=len(lane.queue),
                stop_requested=lane.stop_requested,
            )

    async def request_stop(self, session_id: str) -> bool:
        """Set the cooperative stop flag; cycle cancellation is future work."""

        session_id = self._session_id(session_id)
        async with self._guard:
            lane = self._lanes.setdefault(session_id, _SessionLane())
            changed = not lane.stop_requested
            lane.stop_requested = True
            return changed

    async def reset_session(self, session_id: str) -> int:
        """Invalidate queued work and advance the session generation."""

        session_id = self._session_id(session_id)
        async with self._guard:
            lane = self._lanes.setdefault(session_id, _SessionLane())
            lane.generation += 1
            lane.stop_requested = True
            cancelled = list(lane.queue)
            lane.queue.clear()
            generation = lane.generation
        for item in cancelled:
            if not item.future.done():
                item.future.set_exception(
                    SessionExecutionReset(
                        "queued batch was invalidated by session reset"
                    )
                )
        return generation

    async def shutdown(self) -> None:
        async with self._guard:
            self._closing = True
            workers = [
                lane.worker
                for lane in self._lanes.values()
                if lane.worker is not None and not lane.worker.done()
            ]
            queued = [
                item
                for lane in self._lanes.values()
                for item in lane.queue
            ]
            for lane in self._lanes.values():
                lane.queue.clear()
        for item in queued:
            if not item.future.done():
                item.future.cancel()
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
