"""Telegram-specific bridge for durable ingress and exact artifact delivery."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from tempfile import SpooledTemporaryFile
from typing import Any

import httpx
from telegram.error import BadRequest, NetworkError, TimedOut

from ...adapters.telegram_adapter import TelegramAdapter


logger = logging.getLogger("TelegramArtifactBridge")


class TelegramArtifactBridgeError(RuntimeError):
    """Telegram artifact workflow failed before a safe final state was known."""


@dataclass(slots=True)
class TelegramFileStream:
    size_bytes: int | None
    iterator: AsyncIterator[bytes]


@dataclass(slots=True)
class TelegramDeliveryOutcome:
    delivery_id: str
    state: str
    telegram_message_id: int | None = None
    error: str | None = None


def telegram_session_id(chat_id: int | str, thread_id: int | str | None) -> str:
    suffix = f":thread:{thread_id}" if thread_id is not None else ""
    return f"telegram:conversation:{chat_id}{suffix}"


def _safe_filename(value: str | None, fallback: str) -> str:
    if value is None:
        return fallback
    normalized = value.replace("\\", "/").split("/")[-1]
    normalized = re.sub(r"[\x00-\x1f\x7f]", "", normalized).strip()
    return normalized[:240] or fallback


def extract_telegram_attachments(message: Any) -> list[dict[str, Any]]:
    """Extract one exact Telegram file locator without downloading bytes."""

    message_id = str(getattr(message, "message_id", "file"))

    document = getattr(message, "document", None)
    if document is not None:
        return [{
            "file_id": document.file_id,
            "file_unique_id": getattr(document, "file_unique_id", None),
            "filename": _safe_filename(
                getattr(document, "file_name", None),
                f"document-{message_id}.bin",
            ),
            "mime_type": getattr(document, "mime_type", None),
            "size_bytes": getattr(document, "file_size", None),
            "media_kind": "document",
        }]

    animation = getattr(message, "animation", None)
    if animation is not None:
        return [{
            "file_id": animation.file_id,
            "file_unique_id": getattr(animation, "file_unique_id", None),
            "filename": _safe_filename(
                getattr(animation, "file_name", None),
                f"animation-{message_id}.mp4",
            ),
            "mime_type": getattr(animation, "mime_type", None) or "video/mp4",
            "size_bytes": getattr(animation, "file_size", None),
            "media_kind": "animation",
        }]

    audio = getattr(message, "audio", None)
    if audio is not None:
        return [{
            "file_id": audio.file_id,
            "file_unique_id": getattr(audio, "file_unique_id", None),
            "filename": _safe_filename(
                getattr(audio, "file_name", None),
                f"audio-{message_id}.bin",
            ),
            "mime_type": getattr(audio, "mime_type", None),
            "size_bytes": getattr(audio, "file_size", None),
            "media_kind": "audio",
        }]

    video = getattr(message, "video", None)
    if video is not None:
        return [{
            "file_id": video.file_id,
            "file_unique_id": getattr(video, "file_unique_id", None),
            "filename": _safe_filename(
                getattr(video, "file_name", None),
                f"video-{message_id}.mp4",
            ),
            "mime_type": getattr(video, "mime_type", None) or "video/mp4",
            "size_bytes": getattr(video, "file_size", None),
            "media_kind": "video",
        }]

    voice = getattr(message, "voice", None)
    if voice is not None:
        return [{
            "file_id": voice.file_id,
            "file_unique_id": getattr(voice, "file_unique_id", None),
            "filename": f"voice-{message_id}.ogg",
            "mime_type": getattr(voice, "mime_type", None) or "audio/ogg",
            "size_bytes": getattr(voice, "file_size", None),
            "media_kind": "voice",
        }]

    video_note = getattr(message, "video_note", None)
    if video_note is not None:
        return [{
            "file_id": video_note.file_id,
            "file_unique_id": getattr(video_note, "file_unique_id", None),
            "filename": f"video-note-{message_id}.mp4",
            "mime_type": "video/mp4",
            "size_bytes": getattr(video_note, "file_size", None),
            "media_kind": "video_note",
        }]

    photos = list(getattr(message, "photo", None) or [])
    if photos:
        photo = max(
            photos,
            key=lambda item: (
                int(getattr(item, "file_size", 0) or 0),
                int(getattr(item, "width", 0) or 0)
                * int(getattr(item, "height", 0) or 0),
            ),
        )
        return [{
            "file_id": photo.file_id,
            "file_unique_id": getattr(photo, "file_unique_id", None),
            "filename": f"photo-{message_id}.jpg",
            "mime_type": "image/jpeg",
            "size_bytes": getattr(photo, "file_size", None),
            "media_kind": "photo",
        }]

    return []


def build_telegram_input_envelope(
    update: Any,
    *,
    bot_instance_id: str,
    response_metadata: dict[str, Any] | None = None,
):
    message = update.effective_message
    if message is None:
        raise TelegramArtifactBridgeError("Telegram update has no effective message")
    attachments = extract_telegram_attachments(message)
    if not attachments:
        raise TelegramArtifactBridgeError("Telegram message has no supported attachment")
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        raise TelegramArtifactBridgeError("Telegram update authority is unavailable")

    reply = getattr(message, "reply_to_message", None)
    return TelegramAdapter.build_input_envelope(
        bot_instance_id=bot_instance_id,
        update_id=str(update.update_id),
        chat_id=str(chat.id),
        user_id=str(user.id),
        user_name=getattr(user, "full_name", None),
        message_id=str(message.message_id),
        attachments=attachments,
        caption=getattr(message, "caption", None),
        media_group_id=getattr(message, "media_group_id", None),
        message_thread_id=getattr(message, "message_thread_id", None),
        reply_to_message_id=(
            str(reply.message_id) if reply is not None else None
        ),
        occurred_at=getattr(message, "date", None),
        locale=getattr(user, "language_code", None),
        response_metadata=response_metadata,
    )


class DebouncedBatchRunner:
    """Debounce media groups without cancelling a callback already running."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._running: set[str] = set()
        self._lock = asyncio.Lock()

    async def schedule(
        self,
        key: str,
        *,
        delay_seconds: float,
        callback: Callable[[], Awaitable[None]],
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
                self._worker(key, max(0.0, delay_seconds), callback)
            )
            self._tasks[key] = task
            return True

    async def _worker(
        self,
        key: str,
        delay_seconds: float,
        callback: Callable[[], Awaitable[None]],
    ) -> None:
        try:
            await asyncio.sleep(delay_seconds)
            async with self._lock:
                if self._tasks.get(key) is not asyncio.current_task():
                    return
                self._running.add(key)
            await callback()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Debounced Telegram media-group callback failed")
        finally:
            async with self._lock:
                self._running.discard(key)
                if self._tasks.get(key) is asyncio.current_task():
                    self._tasks.pop(key, None)

    async def cancel_all(self) -> None:
        async with self._lock:
            tasks = list(self._tasks.values())
            self._tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass


class TelegramArtifactGatewayClient:
    """Closed HTTP client for Gateway ingress, commit and delivery routes."""

    def __init__(
        self,
        *,
        gateway_url: str,
        api_key: str,
        transport: httpx.AsyncBaseTransport | None = None,
        delivery_spool_memory_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        self.gateway_url = gateway_url.rstrip("/")
        self.api_key = api_key
        self.transport = transport
        self.delivery_spool_memory_bytes = delivery_spool_memory_bytes

    def _client(self, *, read_timeout: float = 1800.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=read_timeout,
                write=120.0,
                pool=10.0,
            ),
            transport=self.transport,
            headers={"X-API-Key": self.api_key},
        )

    async def submit_envelope(
        self,
        envelope,
        *,
        progress_locale: str,
    ) -> dict[str, Any]:
        async with self._client(read_timeout=180.0) as client:
            response = await client.post(
                f"{self.gateway_url}/ingress/events",
                params={"run": "false", "progress_locale": progress_locale},
                json=envelope.model_dump(mode="json"),
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise TelegramArtifactBridgeError("Gateway ingress response is invalid")
            return payload

    async def commit_and_run(
        self,
        input_batch_id: str,
        *,
        session_id: str,
        progress_locale: str,
    ) -> dict[str, Any]:
        async with self._client() as client:
            response = await client.post(
                f"{self.gateway_url}/input-batches/{input_batch_id}/commit",
                json={
                    "session_id": session_id,
                    "progress_locale": progress_locale,
                    "run": True,
                },
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise TelegramArtifactBridgeError("Gateway commit response is invalid")
            return payload

    async def open_telegram_file(
        self,
        bot: Any,
        file_id: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> TelegramFileStream:
        if not file_id.strip() or len(file_id) > 1024 or any(
            character in file_id for character in "\r\n"
        ):
            raise TelegramArtifactBridgeError("Invalid Telegram file ID")
        telegram_file = await bot.get_file(file_id)
        file_path = str(getattr(telegram_file, "file_path", "") or "")
        if not file_path.startswith(("https://", "http://")):
            raise TelegramArtifactBridgeError("Telegram returned no downloadable file URL")
        size = getattr(telegram_file, "file_size", None)

        async def iterator() -> AsyncIterator[bytes]:
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(10.0, read=180.0),
                    transport=transport,
                ) as client:
                    async with client.stream("GET", file_path) as response:
                        response.raise_for_status()
                        async for chunk in response.aiter_bytes(64 * 1024):
                            if chunk:
                                yield chunk
            except httpx.HTTPError as error:
                raise TelegramArtifactBridgeError(
                    "Telegram file download failed"
                ) from error

        return TelegramFileStream(size_bytes=size, iterator=iterator())

    async def deliver_selected(
        self,
        *,
        bot: Any,
        artifacts: list[dict[str, Any]],
        session_id: str,
        chat_id: int,
        message_thread_id: int | None = None,
        reply_to_message_id: int | None = None,
    ) -> list[TelegramDeliveryOutcome]:
        outcomes: list[TelegramDeliveryOutcome] = []
        for item in artifacts:
            if not isinstance(item, dict):
                continue
            delivery_id = str(item.get("delivery_id") or "")
            if not delivery_id:
                continue
            raw_state = item.get("state")
            state = str(getattr(raw_state, "value", raw_state) or "selected")
            if state != "selected":
                outcomes.append(TelegramDeliveryOutcome(
                    delivery_id=delivery_id,
                    state=state,
                ))
                continue
            outcomes.append(await self._deliver_one(
                bot=bot,
                delivery_id=delivery_id,
                session_id=session_id,
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                reply_to_message_id=reply_to_message_id,
            ))
        return outcomes

    async def _deliver_one(
        self,
        *,
        bot: Any,
        delivery_id: str,
        session_id: str,
        chat_id: int,
        message_thread_id: int | None,
        reply_to_message_id: int | None,
    ) -> TelegramDeliveryOutcome:
        spool = SpooledTemporaryFile(
            max_size=self.delivery_spool_memory_bytes,
            mode="w+b",
        )
        filename = "artifact.bin"
        sent_message: Any | None = None
        receipt: dict[str, Any] = {"provider": "telegram", "chat_id": chat_id}
        try:
            async with self._client(read_timeout=300.0) as client:
                async with client.stream(
                    "GET",
                    f"{self.gateway_url}/internal/deliveries/{delivery_id}/content",
                    params={
                        "session_id": session_id,
                        "client_type": "telegram",
                    },
                ) as response:
                    response.raise_for_status()
                    disposition = response.headers.get("content-disposition", "")
                    filename = _filename_from_disposition(disposition) or filename
                    expected_hash = response.headers.get("x-content-hash")
                    expected_size = _optional_int(
                        response.headers.get("content-length")
                    )
                    digest = hashlib.sha256()
                    total = 0
                    async for chunk in response.aiter_bytes(64 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        digest.update(chunk)
                        spool.write(chunk)
                    if expected_size is not None and total != expected_size:
                        raise TelegramArtifactBridgeError(
                            "Delivery length changed during transport"
                        )
                    actual_hash = "sha256:" + digest.hexdigest()
                    if expected_hash and actual_hash != expected_hash:
                        raise TelegramArtifactBridgeError(
                            "Delivery hash changed during transport"
                        )

            spool.seek(0)
            kwargs: dict[str, Any] = {
                "chat_id": chat_id,
                "document": spool,
                "filename": filename,
            }
            if message_thread_id is not None:
                kwargs["message_thread_id"] = message_thread_id
            if reply_to_message_id is not None:
                kwargs["reply_to_message_id"] = reply_to_message_id
            sent_message = await bot.send_document(**kwargs)
            receipt["message_id"] = getattr(sent_message, "message_id", None)
            document = getattr(sent_message, "document", None)
            if document is not None:
                receipt["telegram_file_id"] = getattr(document, "file_id", None)
                receipt["telegram_file_unique_id"] = getattr(
                    document,
                    "file_unique_id",
                    None,
                )
            try:
                await self._complete(delivery_id, session_id, receipt)
            except Exception as error:
                logger.warning(
                    "Telegram accepted delivery %s but completion receipt failed: %r",
                    delivery_id,
                    error,
                )
                try:
                    await self._fail(
                        delivery_id,
                        session_id,
                        error="completion_receipt_failed",
                        ambiguous=True,
                        receipt=receipt,
                    )
                except Exception:
                    logger.exception(
                        "Failed to persist ambiguous Telegram delivery receipt"
                    )
                return TelegramDeliveryOutcome(
                    delivery_id=delivery_id,
                    state="unknown",
                    telegram_message_id=getattr(sent_message, "message_id", None),
                    error="completion_receipt_failed",
                )
            return TelegramDeliveryOutcome(
                delivery_id=delivery_id,
                state="delivered",
                telegram_message_id=getattr(sent_message, "message_id", None),
            )
        except (TimedOut, NetworkError) as error:
            try:
                await self._fail(
                    delivery_id,
                    session_id,
                    error=type(error).__name__,
                    ambiguous=True,
                    receipt=receipt,
                )
            except Exception:
                logger.exception("Failed to persist ambiguous Telegram timeout")
            return TelegramDeliveryOutcome(
                delivery_id=delivery_id,
                state="unknown",
                telegram_message_id=(
                    getattr(sent_message, "message_id", None)
                    if sent_message is not None
                    else None
                ),
                error=type(error).__name__,
            )
        except BadRequest as error:
            try:
                await self._fail(
                    delivery_id,
                    session_id,
                    error=str(error),
                    ambiguous=False,
                    receipt=receipt,
                )
            except Exception:
                logger.exception("Failed to persist Telegram BadRequest")
            return TelegramDeliveryOutcome(
                delivery_id=delivery_id,
                state="failed",
                error=str(error),
            )
        except Exception as error:
            ambiguous = sent_message is not None
            try:
                await self._fail(
                    delivery_id,
                    session_id,
                    error=type(error).__name__,
                    ambiguous=ambiguous,
                    receipt=receipt,
                )
            except Exception:
                logger.exception("Failed to persist Telegram delivery failure")
            return TelegramDeliveryOutcome(
                delivery_id=delivery_id,
                state="unknown" if ambiguous else "failed",
                telegram_message_id=(
                    getattr(sent_message, "message_id", None)
                    if sent_message is not None
                    else None
                ),
                error=type(error).__name__,
            )
        finally:
            spool.close()

    async def _complete(
        self,
        delivery_id: str,
        session_id: str,
        receipt: dict[str, Any],
    ) -> None:
        async with self._client(read_timeout=30.0) as client:
            response = await client.post(
                f"{self.gateway_url}/internal/deliveries/{delivery_id}/complete",
                json={
                    "session_id": session_id,
                    "client_type": "telegram",
                    "receipt": receipt,
                },
            )
            response.raise_for_status()

    async def _fail(
        self,
        delivery_id: str,
        session_id: str,
        *,
        error: str,
        ambiguous: bool,
        receipt: dict[str, Any] | None = None,
    ) -> None:
        payload_receipt = {"provider": "telegram", **dict(receipt or {})}
        async with self._client(read_timeout=30.0) as client:
            response = await client.post(
                f"{self.gateway_url}/internal/deliveries/{delivery_id}/failed",
                json={
                    "session_id": session_id,
                    "client_type": "telegram",
                    "receipt": payload_receipt,
                    "error": error[:2_000],
                    "ambiguous": ambiguous,
                },
            )
            response.raise_for_status()


def _optional_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _filename_from_disposition(value: str) -> str | None:
    marker = "filename*=UTF-8''"
    if marker not in value:
        return None
    from urllib.parse import unquote

    return _safe_filename(unquote(value.split(marker, 1)[1]), "artifact.bin")
