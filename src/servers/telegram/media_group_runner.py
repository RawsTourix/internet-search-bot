"""Lifetime-bounded Telegram media-group coordination."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

from .artifact_bridge import (
    DebouncedBatchRunner,
    MediaGroupActivityCoordinator,
)


logger = logging.getLogger("TelegramServer.MediaGroupRunner")


class MediaGroupLifetimeExceeded(RuntimeError):
    """The Telegram album stayed open beyond its configured lifetime."""


class LifetimeMediaGroupActivityCoordinator(MediaGroupActivityCoordinator):
    """Track the first member time and suppress late terminal callbacks."""

    def __init__(self, *, tombstone_ttl_seconds: float = 600.0) -> None:
        super().__init__()
        self._lifetime_lock = asyncio.Lock()
        self._opened_at: dict[str, float] = {}
        self._closed_at: dict[str, float] = {}
        self._tombstone_ttl_seconds = max(1.0, tombstone_ttl_seconds)

    async def member_started(self, key: str, *, filename: str | None = None) -> None:
        async with self._lifetime_lock:
            self._prune_tombstones_locked()
            if key in self._closed_at:
                logger.warning(
                    "telegram_media_group_late_member_ignored group_key=%s "
                    "filename=%s phase=started",
                    key,
                    filename,
                )
                return
            self._opened_at.setdefault(key, time.monotonic())
        await super().member_started(key, filename=filename)

    async def member_finished(
        self,
        key: str,
        *,
        filename: str | None = None,
        error: BaseException | str | None = None,
    ) -> None:
        async with self._lifetime_lock:
            self._prune_tombstones_locked()
            if key in self._closed_at:
                logger.info(
                    "telegram_media_group_late_member_ignored group_key=%s "
                    "filename=%s phase=finished",
                    key,
                    filename,
                )
                return
        await super().member_finished(key, filename=filename, error=error)

    async def remaining_lifetime(
        self,
        key: str,
        *,
        maximum_lifetime_seconds: float,
    ) -> float:
        async with self._lifetime_lock:
            opened_at = self._opened_at.get(key)
        if opened_at is None:
            return maximum_lifetime_seconds
        return maximum_lifetime_seconds - (time.monotonic() - opened_at)

    async def close(
        self,
        key: str,
        *,
        ignore_late_members: bool,
    ) -> None:
        await super().clear(key)
        async with self._lifetime_lock:
            self._opened_at.pop(key, None)
            if ignore_late_members:
                self._closed_at[key] = time.monotonic()
            else:
                self._closed_at.pop(key, None)
            self._prune_tombstones_locked()

    def _prune_tombstones_locked(self) -> None:
        threshold = time.monotonic() - self._tombstone_ttl_seconds
        expired = [
            key
            for key, closed_at in self._closed_at.items()
            if closed_at < threshold
        ]
        for key in expired:
            self._closed_at.pop(key, None)


class LifetimeBoundDebouncedBatchRunner(DebouncedBatchRunner):
    """Wait for quiet readiness but fail an album that never terminates."""

    def __init__(
        self,
        *,
        maximum_lifetime_seconds: float,
        activity: LifetimeMediaGroupActivityCoordinator | None = None,
    ) -> None:
        if maximum_lifetime_seconds <= 0:
            raise ValueError("media-group maximum lifetime must be positive")
        coordinator = activity or LifetimeMediaGroupActivityCoordinator()
        super().__init__(activity=coordinator)
        self._activity = coordinator
        self.maximum_lifetime_seconds = maximum_lifetime_seconds

    async def schedule(
        self,
        key: str,
        *,
        delay_seconds: float,
        callback: Callable[[], Awaitable[None]],
        timeout_callback: Callable[[], Awaitable[None]] | None = None,
        reset: bool = True,
    ) -> bool:
        async with self._lock:
            if key in self._running:
                return False
            current = self._tasks.get(key)
            if current is not None and not current.done():
                if not reset:
                    return False
                current.cancel()
            task = asyncio.create_task(
                self._worker(
                    key,
                    max(0.0, delay_seconds),
                    callback,
                    timeout_callback,
                )
            )
            self._tasks[key] = task
            return True

    async def _worker(
        self,
        key: str,
        delay_seconds: float,
        callback: Callable[[], Awaitable[None]],
        timeout_callback: Callable[[], Awaitable[None]] | None,
    ) -> None:
        ignore_late_members = False
        try:
            await asyncio.sleep(delay_seconds)
            async with self._lock:
                if self._tasks.get(key) is not asyncio.current_task():
                    return

            remaining = await self._activity.remaining_lifetime(
                key,
                maximum_lifetime_seconds=self.maximum_lifetime_seconds,
            )
            if remaining <= 0:
                raise MediaGroupLifetimeExceeded(key)
            try:
                ready = await asyncio.wait_for(
                    self._activity.wait_until_ready(
                        key,
                        quiet_period_seconds=delay_seconds,
                    ),
                    timeout=remaining,
                )
            except asyncio.TimeoutError as error:
                raise MediaGroupLifetimeExceeded(key) from error

            if not ready:
                ignore_late_members = True
                return

            async with self._lock:
                if self._tasks.get(key) is not asyncio.current_task():
                    return
                self._running.add(key)
            await callback()
        except asyncio.CancelledError:
            raise
        except MediaGroupLifetimeExceeded:
            ignore_late_members = True
            snapshot = await self._activity.snapshot(key)
            logger.error(
                "telegram_media_group_lifetime_exceeded group_key=%s "
                "maximum_lifetime_seconds=%s state=%s",
                key,
                self.maximum_lifetime_seconds,
                snapshot,
            )
            if timeout_callback is not None:
                try:
                    await timeout_callback()
                except Exception:
                    logger.exception(
                        "telegram_media_group_timeout_callback_failed "
                        "group_key=%s",
                        key,
                    )
        except Exception:
            ignore_late_members = True
            logger.exception(
                "telegram_media_group_callback_failed group_key=%s",
                key,
            )
        finally:
            should_close_activity = False
            async with self._lock:
                self._running.discard(key)
                if self._tasks.get(key) is asyncio.current_task():
                    self._tasks.pop(key, None)
                    should_close_activity = True
            if should_close_activity:
                await self._activity.close(
                    key,
                    ignore_late_members=ignore_late_members,
                )

    async def cancel_all(self) -> None:
        async with self._lock:
            items = list(self._tasks.items())
            self._tasks.clear()
            self._running.clear()
        for _, task in items:
            if not task.done():
                task.cancel()
        for _, task in items:
            try:
                await task
            except asyncio.CancelledError:
                pass
        for key, _ in items:
            await self._activity.close(key, ignore_late_members=False)
