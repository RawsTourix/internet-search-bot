"""Reusable deterministic Telegram transport fakes."""

from __future__ import annotations

from collections import defaultdict, deque
from io import BytesIO
from types import SimpleNamespace
from typing import Any

from telegram import InputFile


class FakeTelegramBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._message_id = 100
        self.behaviors: dict[str, deque[Any]] = defaultdict(deque)

    def queue(self, method: str, *behaviors: Any) -> None:
        self.behaviors[method].extend(behaviors)

    async def _call(self, method: str, **kwargs: Any):
        self.calls.append((method, kwargs))
        behavior = (
            self.behaviors[method].popleft()
            if self.behaviors[method]
            else None
        )
        if isinstance(behavior, BaseException):
            raise behavior
        if method == "send_media_group":
            count = len(kwargs["media"])
            if isinstance(behavior, int):
                count = behavior
            return [self._message() for _ in range(count)]
        return self._message(
            message_id=(
                behavior if isinstance(behavior, int) else None
            )
        )

    def _message(self, *, message_id: int | None = None):
        if message_id is None:
            self._message_id += 1
            message_id = self._message_id
        return SimpleNamespace(message_id=message_id)

    async def send_message(self, **kwargs):
        return await self._call("send_message", **kwargs)

    async def edit_message_text(self, **kwargs):
        return await self._call("edit_message_text", **kwargs)

    async def send_document(self, **kwargs):
        return await self._call("send_document", **kwargs)

    async def send_media_group(self, **kwargs):
        return await self._call("send_media_group", **kwargs)

    async def send_photo(self, **kwargs):
        return await self._call("send_photo", **kwargs)

    async def send_audio(self, **kwargs):
        return await self._call("send_audio", **kwargs)

    async def send_voice(self, **kwargs):
        return await self._call("send_voice", **kwargs)

    async def send_video(self, **kwargs):
        return await self._call("send_video", **kwargs)

    async def send_video_note(self, **kwargs):
        return await self._call("send_video_note", **kwargs)

    async def send_animation(self, **kwargs):
        return await self._call("send_animation", **kwargs)

    async def send_sticker(self, **kwargs):
        return await self._call("send_sticker", **kwargs)

    async def send_location(self, **kwargs):
        return await self._call("send_location", **kwargs)

    async def send_contact(self, **kwargs):
        return await self._call("send_contact", **kwargs)


class FakeTelegramGateway:
    def __init__(self) -> None:
        self.opened: list[str] = []

    async def open_delivery_file(self, delivery_id: str, *, session_id: str):
        from tempfile import SpooledTemporaryFile

        self.opened.append(delivery_id)
        spool = SpooledTemporaryFile(mode="w+b")
        spool.write(delivery_id.encode("utf-8"))
        spool.seek(0)
        return spool, f"{delivery_id}.bin"

    @staticmethod
    def telegram_input_file(spool, filename: str):
        return InputFile(
            spool,
            filename=filename,
            read_file_handle=False,
        )
