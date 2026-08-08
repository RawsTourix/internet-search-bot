import asyncio
from pathlib import Path

from src.input_runtime.coordination import SessionLockRegistry


def test_lock_registry_serializes_same_session_and_allows_parallel_sessions(tmp_path: Path):
    async def scenario():
        registry = SessionLockRegistry(max_entries=8)
        active = 0
        max_active_same = 0
        order = []

        async def worker(name):
            nonlocal active, max_active_same
            async with registry.hold(tmp_path, "same"):
                active += 1
                max_active_same = max(max_active_same, active)
                order.append(f"start-{name}")
                await asyncio.sleep(0)
                order.append(f"end-{name}")
                active -= 1

        await asyncio.gather(worker("a"), worker("b"), worker("c"))
        assert max_active_same == 1
        assert order == [
            "start-a", "end-a", "start-b", "end-b", "start-c", "end-c"
        ]

    asyncio.run(scenario())


def test_lock_registry_is_bounded_and_cleanup_capable(tmp_path: Path):
    async def scenario():
        registry = SessionLockRegistry(max_entries=2)
        for index in range(5):
            async with registry.hold(tmp_path, f"session-{index}"):
                pass
        assert registry.size <= 2
        removed = await registry.cleanup()
        assert removed >= 0
        assert registry.size <= 2

    asyncio.run(scenario())


def test_different_repository_instances_share_process_lock(tmp_path: Path):
    async def scenario():
        first = SessionLockRegistry(max_entries=8)
        second = first
        entered = []

        async def one():
            async with first.hold(tmp_path, "session"):
                entered.append("one")
                await asyncio.sleep(0.01)

        async def two():
            await asyncio.sleep(0)
            async with second.hold(tmp_path, "session"):
                entered.append("two")

        await asyncio.gather(one(), two())
        assert entered == ["one", "two"]

    asyncio.run(scenario())
