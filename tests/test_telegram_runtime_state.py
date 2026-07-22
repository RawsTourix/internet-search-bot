import asyncio
import unittest

from src.servers.telegram.runtime_state import KeyedAsyncLockPool


class TelegramRuntimeStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_key_is_serialized_and_entry_is_released(self):
        pool = KeyedAsyncLockPool()
        entered = []
        first_inside = asyncio.Event()
        release_first = asyncio.Event()

        async def first():
            async with pool.hold("message-1"):
                entered.append("first")
                first_inside.set()
                await release_first.wait()

        async def second():
            await first_inside.wait()
            async with pool.hold("message-1"):
                entered.append("second")

        first_task = asyncio.create_task(first())
        second_task = asyncio.create_task(second())
        await first_inside.wait()
        await asyncio.sleep(0)
        self.assertEqual(entered, ["first"])
        self.assertEqual(await pool.size(), 1)

        release_first.set()
        await asyncio.gather(first_task, second_task)
        self.assertEqual(entered, ["first", "second"])
        self.assertEqual(await pool.size(), 0)

    async def test_different_keys_can_run_concurrently(self):
        pool = KeyedAsyncLockPool()
        both_inside = asyncio.Event()
        release = asyncio.Event()
        inside = 0
        guard = asyncio.Lock()

        async def worker(key: str):
            nonlocal inside
            async with pool.hold(key):
                async with guard:
                    inside += 1
                    if inside == 2:
                        both_inside.set()
                await release.wait()
                async with guard:
                    inside -= 1

        tasks = [
            asyncio.create_task(worker("a")),
            asyncio.create_task(worker("b")),
        ]
        await asyncio.wait_for(both_inside.wait(), timeout=1)
        self.assertEqual(await pool.size(), 2)
        release.set()
        await asyncio.gather(*tasks)
        self.assertEqual(await pool.size(), 0)

    async def test_empty_key_is_rejected(self):
        pool = KeyedAsyncLockPool()
        with self.assertRaises(ValueError):
            async with pool.hold("   "):
                pass
        self.assertEqual(await pool.size(), 0)


if __name__ == "__main__":
    unittest.main()
