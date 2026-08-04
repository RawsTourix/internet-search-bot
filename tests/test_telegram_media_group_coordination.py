import asyncio
import unittest
from types import SimpleNamespace

from src.servers.telegram.artifact_bridge import (
    DebouncedBatchRunner,
    MediaGroupActivityCoordinator,
    telegram_media_group_key,
)
from src.servers.telegram.media_group_runner import (
    LifetimeBoundDebouncedBatchRunner,
    LifetimeMediaGroupActivityCoordinator,
)


class TelegramMediaGroupCoordinationTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_abort_fences_late_media_group_members(self):
        activity = LifetimeMediaGroupActivityCoordinator()
        runner = LifetimeBoundDebouncedBatchRunner(
            activity=activity,
            maximum_lifetime_seconds=1,
        )
        called = asyncio.Event()
        async def callback():
            called.set()

        await runner.schedule(
            "bot:chat:-:album",
            delay_seconds=0.1,
            callback=callback,
        )
        await runner.abort("bot:chat:-:album")
        self.assertTrue(await runner.is_closed("bot:chat:-:album"))
        await activity.member_started("bot:chat:-:album", filename="late.md")
        self.assertIsNone(await activity.snapshot("bot:chat:-:album"))
        self.assertFalse(called.is_set())

    async def test_runner_waits_for_active_member_and_new_quiet_period(self):
        activity = MediaGroupActivityCoordinator()
        runner = DebouncedBatchRunner(activity=activity)
        callback_started = asyncio.Event()
        key = "bot:chat:-:album"

        async def callback():
            callback_started.set()

        await activity.member_started(key, filename="slow.txt")
        await runner.schedule(
            key,
            delay_seconds=0.03,
            callback=callback,
        )

        await asyncio.sleep(0.06)
        self.assertFalse(callback_started.is_set())

        await activity.member_finished(key, filename="slow.txt")
        await asyncio.sleep(0.015)
        self.assertFalse(callback_started.is_set())

        await asyncio.wait_for(callback_started.wait(), timeout=1)
        await runner.cancel_all()

    async def test_late_member_resets_quiet_period_and_is_awaited(self):
        activity = MediaGroupActivityCoordinator()
        runner = DebouncedBatchRunner(activity=activity)
        callback_started = asyncio.Event()
        key = "bot:chat:-:album"

        async def callback():
            callback_started.set()

        await activity.member_started(key, filename="first.txt")
        await activity.member_finished(key, filename="first.txt")
        await runner.schedule(
            key,
            delay_seconds=0.05,
            callback=callback,
        )

        await asyncio.sleep(0.02)
        await activity.member_started(key, filename="late.txt")
        await asyncio.sleep(0.06)
        self.assertFalse(callback_started.is_set())

        await activity.member_finished(key, filename="late.txt")
        await asyncio.sleep(0.02)
        self.assertFalse(callback_started.is_set())

        await asyncio.wait_for(callback_started.wait(), timeout=1)
        await runner.cancel_all()

    async def test_failed_member_suppresses_commit_callback(self):
        activity = MediaGroupActivityCoordinator()
        runner = DebouncedBatchRunner(activity=activity)
        callback_calls = []
        key = "bot:chat:-:album"

        async def callback():
            callback_calls.append("called")

        await activity.member_started(key, filename="failed.txt")
        await runner.schedule(
            key,
            delay_seconds=0.01,
            callback=callback,
        )
        await activity.member_finished(
            key,
            filename="failed.txt",
            error=RuntimeError("download failed"),
        )

        for _ in range(100):
            if await activity.snapshot(key) is None:
                break
            await asyncio.sleep(0.01)
        else:
            self.fail("failed media-group activity was not released")

        self.assertEqual(callback_calls, [])
        await runner.cancel_all()

    async def test_rescheduled_worker_does_not_clear_live_activity(self):
        activity = MediaGroupActivityCoordinator()
        runner = DebouncedBatchRunner(activity=activity)
        callback_started = asyncio.Event()
        key = "bot:chat:-:album"

        async def callback():
            callback_started.set()

        await activity.member_started(key, filename="one.txt")
        await runner.schedule(
            key,
            delay_seconds=0.05,
            callback=callback,
        )
        await runner.schedule(
            key,
            delay_seconds=0.01,
            callback=callback,
        )
        await asyncio.sleep(0.03)

        snapshot = await activity.snapshot(key)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["in_flight"], 1)
        self.assertFalse(callback_started.is_set())

        await activity.member_finished(key, filename="one.txt")
        await asyncio.wait_for(callback_started.wait(), timeout=1)
        await runner.cancel_all()

    async def test_maximum_lifetime_calls_timeout_without_commit(self):
        activity = LifetimeMediaGroupActivityCoordinator()
        runner = LifetimeBoundDebouncedBatchRunner(
            activity=activity,
            maximum_lifetime_seconds=0.06,
        )
        commit_called = asyncio.Event()
        timeout_called = asyncio.Event()
        key = "bot:chat:-:stalled-album"

        async def commit_callback():
            commit_called.set()

        async def timeout_callback():
            timeout_called.set()

        await activity.member_started(key, filename="stalled.txt")
        await runner.schedule(
            key,
            delay_seconds=0.01,
            callback=commit_callback,
            timeout_callback=timeout_callback,
        )

        await asyncio.wait_for(timeout_called.wait(), timeout=1)
        self.assertFalse(commit_called.is_set())
        self.assertIsNone(await activity.snapshot(key))

        # A transport request may finish after the local lifetime expires. Its
        # terminal callback must not recreate the already-closed group state.
        await activity.member_finished(key, filename="stalled.txt")
        self.assertIsNone(await activity.snapshot(key))
        await runner.cancel_all()

    async def test_lifetime_is_measured_from_first_member_not_first_schedule(self):
        activity = LifetimeMediaGroupActivityCoordinator()
        runner = LifetimeBoundDebouncedBatchRunner(
            activity=activity,
            maximum_lifetime_seconds=0.08,
        )
        timeout_called = asyncio.Event()
        key = "bot:chat:-:slow-first-member"

        await activity.member_started(key, filename="slow.txt")
        await asyncio.sleep(0.05)

        await runner.schedule(
            key,
            delay_seconds=0.01,
            callback=lambda: asyncio.sleep(0),
            timeout_callback=lambda: _set_event(timeout_called),
        )

        await asyncio.wait_for(timeout_called.wait(), timeout=1)
        await runner.cancel_all()

    def test_envelope_key_matches_telegram_server_group_key(self):
        envelope = SimpleNamespace(
            source_group_id="album-42",
            client_instance_id="default",
            conversation=SimpleNamespace(
                conversation_id="1062062174",
                thread_id=None,
            ),
        )
        self.assertEqual(
            telegram_media_group_key(envelope),
            "default:1062062174:-:album-42",
        )


async def _set_event(event: asyncio.Event) -> None:
    event.set()


if __name__ == "__main__":
    unittest.main()
