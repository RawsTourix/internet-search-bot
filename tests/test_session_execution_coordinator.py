import asyncio
import time
import unittest

from src.runtime import SessionExecutionCoordinator, SessionExecutionReset


class SessionExecutionCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.coordinator = SessionExecutionCoordinator()

    async def asyncTearDown(self):
        await self.coordinator.shutdown()

    async def test_same_session_committed_batches_are_fifo_and_do_not_overlap(self):
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        intervals: dict[str, tuple[float, float]] = {}

        async def first():
            started = time.monotonic()
            first_started.set()
            await release_first.wait()
            intervals["album"] = (started, time.monotonic())
            return "album"

        async def second():
            started = time.monotonic()
            await asyncio.sleep(0)
            intervals["standalone"] = (started, time.monotonic())
            return "standalone"

        album = asyncio.create_task(self.coordinator.enqueue(
            session_id="telegram:conversation:1",
            input_batch_id="ibat-album",
            operation=first,
        ))
        await first_started.wait()
        standalone = asyncio.create_task(self.coordinator.enqueue(
            session_id="telegram:conversation:1",
            input_batch_id="ibat-standalone",
            operation=second,
        ))
        await asyncio.sleep(0)

        snapshot = await self.coordinator.snapshot("telegram:conversation:1")
        self.assertEqual(snapshot.active_input_batch_id, "ibat-album")
        self.assertEqual(snapshot.queued_batches, 1)
        self.assertFalse(standalone.done())

        release_first.set()
        self.assertEqual(await asyncio.gather(album, standalone), [
            "album",
            "standalone",
        ])
        self.assertLessEqual(
            intervals["album"][1],
            intervals["standalone"][0],
        )

    async def test_run_lease_serializes_callers_that_bypass_fifo(self):
        active = 0
        maximum_active = 0

        async def run(cycle_id: str):
            nonlocal active, maximum_active
            async with self.coordinator.run_lease(
                session_id="session",
                cycle_id=cycle_id,
            ):
                active += 1
                maximum_active = max(maximum_active, active)
                await asyncio.sleep(0.01)
                active -= 1

        await asyncio.gather(run("cycle-1"), run("cycle-2"))
        self.assertEqual(maximum_active, 1)

    async def test_snapshot_is_readable_while_cycle_is_running(self):
        release = asyncio.Event()
        started = asyncio.Event()

        async def operation():
            async with self.coordinator.run_lease(
                session_id="session",
                input_batch_id="ibat-1",
                cycle_id="cycle-1",
            ):
                started.set()
                await release.wait()

        task = asyncio.create_task(self.coordinator.enqueue(
            session_id="session",
            input_batch_id="ibat-1",
            operation=operation,
        ))
        await started.wait()
        snapshot = await asyncio.wait_for(
            self.coordinator.snapshot("session"),
            timeout=0.1,
        )
        self.assertEqual(snapshot.runtime_status, "running")
        self.assertEqual(snapshot.active_cycle_id, "cycle-1")
        self.assertIsNotNone(snapshot.run_seconds)
        release.set()
        await task

    async def test_reset_invalidates_queued_batch(self):
        release = asyncio.Event()
        started = asyncio.Event()

        async def first():
            started.set()
            await release.wait()

        first_task = asyncio.create_task(self.coordinator.enqueue(
            session_id="session",
            input_batch_id="ibat-1",
            operation=first,
        ))
        await started.wait()
        second_task = asyncio.create_task(self.coordinator.enqueue(
            session_id="session",
            input_batch_id="ibat-2",
            operation=lambda: asyncio.sleep(0),
        ))
        await asyncio.sleep(0)
        generation = await self.coordinator.reset_session("session")
        self.assertEqual(generation, 1)
        with self.assertRaises(SessionExecutionReset):
            await second_task
        release.set()
        await first_task


if __name__ == "__main__":
    unittest.main()
