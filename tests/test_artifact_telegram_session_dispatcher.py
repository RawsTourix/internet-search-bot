import asyncio
import unittest

from src.servers.telegram.runtime_state import TelegramSessionDispatcher


class TelegramSessionDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dispatcher = TelegramSessionDispatcher()

    async def asyncTearDown(self):
        await self.dispatcher.shutdown()

    async def test_same_session_runs_in_exact_submission_order(self):
        first_release = asyncio.Event()
        first_started = asyncio.Event()
        order: list[str] = []

        async def first():
            order.append("first-start")
            first_started.set()
            await first_release.wait()
            order.append("first-end")
            return 1

        async def second():
            order.append("second")
            return 2

        first_waiter = asyncio.create_task(
            self.dispatcher.submit("session-1", first)
        )
        second_waiter = asyncio.create_task(
            self.dispatcher.submit("session-1", second)
        )

        await first_started.wait()
        await asyncio.sleep(0)
        self.assertEqual(order, ["first-start"])

        first_release.set()
        self.assertEqual(
            await asyncio.gather(first_waiter, second_waiter),
            [1, 2],
        )
        self.assertEqual(order, ["first-start", "first-end", "second"])
        self.assertEqual(self.dispatcher.lane_count(), 0)

    async def test_different_sessions_remain_concurrent(self):
        blocked_release = asyncio.Event()
        blocked_started = asyncio.Event()
        other_finished = asyncio.Event()

        async def blocked():
            blocked_started.set()
            await blocked_release.wait()

        async def other():
            other_finished.set()
            return "ok"

        blocked_waiter = asyncio.create_task(
            self.dispatcher.submit("session-1", blocked)
        )
        other_waiter = asyncio.create_task(
            self.dispatcher.submit("session-2", other)
        )

        await blocked_started.wait()
        await asyncio.wait_for(other_finished.wait(), timeout=1.0)
        self.assertEqual(await other_waiter, "ok")
        self.assertFalse(blocked_waiter.done())

        blocked_release.set()
        await blocked_waiter

    async def test_shared_data_runs_concurrently_before_command_barrier(self):
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        release_data = asyncio.Event()
        command_started = asyncio.Event()

        async def first_data():
            first_started.set()
            await release_data.wait()
            return "first"

        async def second_data():
            second_started.set()
            await release_data.wait()
            return "second"

        async def command():
            command_started.set()
            return "sent"

        first_waiter = asyncio.create_task(
            self.dispatcher.submit_shared("session-1", first_data)
        )
        second_waiter = asyncio.create_task(
            self.dispatcher.submit_shared("session-1", second_data)
        )
        command_waiter = asyncio.create_task(
            self.dispatcher.submit("session-1", command)
        )

        await asyncio.wait_for(first_started.wait(), timeout=1.0)
        await asyncio.wait_for(second_started.wait(), timeout=1.0)
        self.assertFalse(command_started.is_set())

        release_data.set()
        self.assertEqual(
            await asyncio.gather(first_waiter, second_waiter, command_waiter),
            ["first", "second", "sent"],
        )
        self.assertTrue(command_started.is_set())

    async def test_shared_data_cannot_overtake_waiting_command_barrier(self):
        first_release = asyncio.Event()
        first_started = asyncio.Event()
        command_release = asyncio.Event()
        command_started = asyncio.Event()
        late_data_started = asyncio.Event()

        async def first_data():
            first_started.set()
            await first_release.wait()

        async def command():
            command_started.set()
            await command_release.wait()

        async def late_data():
            late_data_started.set()

        first_waiter = asyncio.create_task(
            self.dispatcher.submit_shared("session-1", first_data)
        )
        await first_started.wait()
        command_waiter = asyncio.create_task(
            self.dispatcher.submit("session-1", command)
        )
        late_data_waiter = asyncio.create_task(
            self.dispatcher.submit_shared("session-1", late_data)
        )

        first_release.set()
        await asyncio.wait_for(command_started.wait(), timeout=1.0)
        self.assertFalse(late_data_started.is_set())

        command_release.set()
        await asyncio.gather(first_waiter, command_waiter, late_data_waiter)
        self.assertTrue(late_data_started.is_set())

    async def test_nested_same_session_operation_runs_inline_without_deadlock(self):
        order: list[str] = []

        async def outer():
            order.append("outer-start")

            async def inner():
                order.append("inner")
                return "inner-result"

            result = await self.dispatcher.run("session-1", inner)
            order.append("outer-end")
            return result

        result = await self.dispatcher.run("session-1", outer)

        self.assertEqual(result, "inner-result")
        self.assertEqual(order, ["outer-start", "inner", "outer-end"])

    async def test_reset_invalidates_old_queued_callbacks(self):
        first_started = asyncio.Event()
        release = asyncio.Event()
        second_called = False

        async def first():
            first_started.set()
            await release.wait()

        async def second():
            nonlocal second_called
            second_called = True

        first_task = asyncio.create_task(
            self.dispatcher.submit("session-1", first)
        )
        await first_started.wait()
        second_task = asyncio.create_task(
            self.dispatcher.submit("session-1", second)
        )
        await asyncio.sleep(0)
        await self.dispatcher.reset_session("session-1")
        with self.assertRaisesRegex(RuntimeError, "invalidated"):
            await second_task
        release.set()
        await first_task
        self.assertFalse(second_called)

    async def test_pending_lane_is_visible_before_first_operation_finishes(self):
        started = asyncio.Event()
        release = asyncio.Event()

        async def operation():
            started.set()
            await release.wait()

        waiter = asyncio.create_task(
            self.dispatcher.submit("session-1", operation)
        )
        await started.wait()
        self.assertTrue(self.dispatcher.has_pending("session-1"))
        self.assertFalse(self.dispatcher.has_pending("session-2"))

        release.set()
        await waiter
        self.assertFalse(self.dispatcher.has_pending("session-1"))


if __name__ == "__main__":
    unittest.main()
