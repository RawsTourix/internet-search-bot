import asyncio
import unittest
from types import SimpleNamespace

from src.servers.telegram.artifact_bridge import (
    DebouncedBatchRunner,
    MediaGroupActivityCoordinator,
    telegram_media_group_key,
)


class TelegramMediaGroupCoordinationTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
