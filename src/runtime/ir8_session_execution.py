"""IR-8 process-local runner ownership over the durable session runtime."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from .session_execution import (
    SessionExecutionCoordinator as _BaseCoordinator,
    SessionExecutionReset,
    _SessionLane,
)


class SessionExecutionCoordinator(_BaseCoordinator):
    """Add fresh-process reservations and active admitted-task ownership.

    The coordinator remains defensive process-local state only.  Generations and
    active cycle identities installed here always come from durable recovery.
    """

    def __init__(self) -> None:
        super().__init__()
        self._active_admitted_tasks: set[asyncio.Task[object]] = set()

    async def install_recovered_reservation(
        self,
        *,
        session_id: str,
        cycle_id: str,
        generation: int,
    ) -> None:
        session_id = self._session_id(session_id)
        cycle_id = cycle_id.strip()
        if not cycle_id or generation < 0:
            raise ValueError("recovered cycle identity/generation is invalid")
        async with self._guard:
            if self._closing:
                raise RuntimeError("session execution coordinator is shutting down")
            lane = self._lanes.setdefault(session_id, _SessionLane())
            if lane.active_cycle_id is not None:
                if lane.active_cycle_id != cycle_id:
                    raise SessionExecutionReset(
                        "fresh process already owns another active cycle"
                    )
                return
            if lane.reserved_cycle_id not in {None, cycle_id}:
                raise SessionExecutionReset(
                    "fresh process already reserves another cycle"
                )
            lane.generation = generation
            lane.reserved_cycle_id = cycle_id
            lane.runtime_status = "recovered_pending"
            lane.stop_requested = False
            lane.wake_event.clear()

    async def release_recovered_reservation(
        self,
        *,
        session_id: str,
        cycle_id: str,
    ) -> None:
        session_id = self._session_id(session_id)
        async with self._guard:
            lane = self._lanes.get(session_id)
            if lane is None:
                return
            if lane.reserved_cycle_id == cycle_id and lane.active_cycle_id is None:
                lane.reserved_cycle_id = None
                lane.runtime_status = "idle"

    @asynccontextmanager
    async def admitted_run_lease(
        self,
        *,
        session_id: str,
        input_batch_id: str,
        cycle_id: str,
        expected_generation: int | None = None,
    ) -> AsyncIterator[bool]:
        """Consume a matching recovery reservation or reserve a normal runner."""

        session_id = self._session_id(session_id)
        input_batch_id = input_batch_id.strip()
        cycle_id = cycle_id.strip()
        if not input_batch_id or not cycle_id:
            raise ValueError("input_batch_id and cycle_id are required")

        generation = 0
        acquired_reservation = False
        generation_matches = True
        current_task = asyncio.current_task()
        async with self._guard:
            if self._closing:
                raise RuntimeError("session execution coordinator is shutting down")
            lane = self._lanes.setdefault(session_id, _SessionLane())
            generation = lane.generation
            generation_matches = (
                expected_generation is None
                or generation == expected_generation
            )
            recovered_match = (
                generation_matches
                and lane.reserved_cycle_id == cycle_id
                and lane.active_cycle_id is None
                and not lane.run_lease.locked()
            )
            normal_available = (
                generation_matches
                and lane.reserved_cycle_id is None
                and lane.active_cycle_id is None
                and not lane.run_lease.locked()
            )
            if recovered_match or normal_available:
                if normal_available:
                    lane.reserved_cycle_id = cycle_id
                lane.active_cycle_id = cycle_id
                lane.active_input_batch_id = input_batch_id
                lane.run_started_at_monotonic = time.monotonic()
                lane.runtime_status = "starting"
                lane.stop_requested = False
                acquired_reservation = True
                if current_task is not None:
                    self._active_admitted_tasks.add(current_task)

        if not generation_matches or not acquired_reservation:
            yield False
            return

        try:
            async with lane.run_lease:
                async with self._guard:
                    if generation != lane.generation:
                        raise SessionExecutionReset(
                            "admitted runner was invalidated before lease acquisition"
                        )
                    if lane.reserved_cycle_id != cycle_id:
                        raise SessionExecutionReset(
                            "admitted runner reservation was lost"
                        )
                    lane.runtime_status = "running"
                    lane.run_started_at_monotonic = time.monotonic()
                    lane.wake_event.clear()
                yield True
        finally:
            async with self._guard:
                if current_task is not None:
                    self._active_admitted_tasks.discard(current_task)
                if lane.reserved_cycle_id == cycle_id:
                    lane.reserved_cycle_id = None
                if lane.active_cycle_id == cycle_id:
                    lane.active_cycle_id = None
                    lane.active_input_batch_id = None
                    lane.run_started_at_monotonic = None
                    lane.runtime_status = "idle"

    async def synchronize_generation(self, session_id: str, *, generation: int) -> int:
        session_id = self._session_id(session_id)
        if generation < 0:
            raise ValueError("generation must be non-negative")
        async with self._guard:
            lane = self._lanes.setdefault(session_id, _SessionLane())
            if lane.generation != generation:
                lane.reserved_cycle_id = None
            lane.generation = generation
            lane.stop_requested = True
            lane.wake_event.set()
            cancelled = list(lane.queue)
            lane.queue.clear()
        for item in cancelled:
            if not item.future.done():
                item.future.set_exception(
                    SessionExecutionReset(
                        "queued batch was invalidated by durable session reset"
                    )
                )
        return generation

    async def shutdown(self) -> None:
        current = asyncio.current_task()
        async with self._guard:
            self._closing = True
            active = [
                task
                for task in self._active_admitted_tasks
                if task is not current and not task.done()
            ]
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
                lane.wake_event.set()
                if lane.active_cycle_id is None:
                    lane.reserved_cycle_id = None
        for item in queued:
            if not item.future.done():
                item.future.cancel()
        for task in active:
            task.cancel()
        for worker in workers:
            worker.cancel()
        pending = [*active, *workers]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
