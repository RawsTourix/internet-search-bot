"""IR-9 Telegram presentation-only ordering and edit fallback.

This layer fences visual writes only. It never mutates durable input/runtime
authority and never creates AgentEmission/OutputBatch records.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from telegram.error import BadRequest, NetworkError, TimedOut


class ProjectionEditDisposition(str, Enum):
    EDITED = "edited"
    FALLBACK_SENT = "fallback_sent"
    SUPPRESSED_STALE = "suppressed_stale"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ProjectionEditResult:
    disposition: ProjectionEditDisposition
    message: Any | None = None


@dataclass(frozen=True, slots=True)
class _Desired:
    session_generation: int
    revision: int


EditCall = Callable[[], Awaitable[Any]]
SendCall = Callable[[], Awaitable[Any]]
GenerationFence = Callable[[], bool]


class TelegramProjectionEditor:
    """Serialize one presentation handle and suppress stale visual revisions."""

    def __init__(self, *, maximum_presentations: int = 2048) -> None:
        if maximum_presentations < 1:
            raise ValueError("maximum_presentations must be positive")
        self.maximum_presentations = int(maximum_presentations)
        self._guard = asyncio.Lock()
        self._locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        self._desired: OrderedDict[str, _Desired] = OrderedDict()
        self._next_revision: dict[str, int] = {}

    async def clear_session(self, session_id: str) -> None:
        prefix = f"{str(session_id).strip()}|"
        async with self._guard:
            for key in tuple(self._desired):
                if key.startswith(prefix):
                    self._desired.pop(key, None)
                    self._next_revision.pop(key, None)
                    self._locks.pop(key, None)

    async def _reserve(
        self,
        *,
        key: str,
        session_generation: int,
        revision: int | None,
    ) -> tuple[_Desired, asyncio.Lock]:
        if session_generation < 0:
            raise ValueError("session_generation must not be negative")
        async with self._guard:
            current = self._desired.get(key)
            if revision is None:
                next_revision = self._next_revision.get(key, 0) + 1
            else:
                next_revision = int(revision)
                if next_revision < 0:
                    raise ValueError("revision must not be negative")
            candidate = _Desired(
                session_generation=int(session_generation),
                revision=next_revision,
            )
            if current is not None:
                if candidate.session_generation < current.session_generation:
                    return candidate, self._locks.setdefault(key, asyncio.Lock())
                if (
                    candidate.session_generation == current.session_generation
                    and candidate.revision <= current.revision
                ):
                    return candidate, self._locks.setdefault(key, asyncio.Lock())
            self._desired[key] = candidate
            self._next_revision[key] = max(
                self._next_revision.get(key, 0),
                next_revision,
            )
            lock = self._locks.setdefault(key, asyncio.Lock())
            self._desired.move_to_end(key)
            self._locks.move_to_end(key)
            while len(self._desired) > self.maximum_presentations:
                old_key, _ = self._desired.popitem(last=False)
                self._next_revision.pop(old_key, None)
                old_lock = self._locks.get(old_key)
                if old_lock is not None and not old_lock.locked():
                    self._locks.pop(old_key, None)
            return candidate, lock

    async def _is_current(self, key: str, candidate: _Desired) -> bool:
        async with self._guard:
            return self._desired.get(key) == candidate

    async def update(
        self,
        *,
        session_id: str,
        presentation_id: str,
        session_generation: int,
        edit: EditCall,
        send_new: SendCall,
        revision: int | None = None,
        generation_is_current: GenerationFence | None = None,
    ) -> ProjectionEditResult:
        normalized_session = str(session_id).strip()
        normalized_presentation = str(presentation_id).strip()
        if not normalized_session or not normalized_presentation:
            raise ValueError("presentation scope must be non-empty")
        key = f"{normalized_session}|{normalized_presentation}"
        candidate, lock = await self._reserve(
            key=key,
            session_generation=session_generation,
            revision=revision,
        )

        def generation_valid() -> bool:
            return generation_is_current is None or bool(generation_is_current())

        if not generation_valid() or not await self._is_current(key, candidate):
            return ProjectionEditResult(ProjectionEditDisposition.SUPPRESSED_STALE)

        # Presentation-local serialization intentionally spans transport await:
        # it is not a runtime/session lock. An older edit that has already
        # entered the network always finishes before a newer desired revision
        # writes the same handle, so the final visible state cannot regress.
        async with lock:
            if not generation_valid() or not await self._is_current(key, candidate):
                return ProjectionEditResult(ProjectionEditDisposition.SUPPRESSED_STALE)
            try:
                message = await edit()
            except BadRequest as error:
                if "message is not modified" in str(error).lower():
                    return ProjectionEditResult(ProjectionEditDisposition.EDITED)
                # Telegram rejected the edit deterministically. Fall back to a
                # new presentation only while this revision/generation is current.
                if not generation_valid() or not await self._is_current(key, candidate):
                    return ProjectionEditResult(ProjectionEditDisposition.SUPPRESSED_STALE)
                try:
                    fallback = await send_new()
                except (TimedOut, NetworkError):
                    # Send outcome itself is ambiguous. Never send another copy.
                    return ProjectionEditResult(ProjectionEditDisposition.UNKNOWN)
                return ProjectionEditResult(
                    ProjectionEditDisposition.FALLBACK_SENT,
                    fallback,
                )
            except (TimedOut, NetworkError):
                # The edit may have reached Telegram. A blind send_new would
                # create a duplicate presentation and is therefore forbidden.
                return ProjectionEditResult(ProjectionEditDisposition.UNKNOWN)

            if not generation_valid() or not await self._is_current(key, candidate):
                return ProjectionEditResult(
                    ProjectionEditDisposition.SUPPRESSED_STALE,
                    message,
                )
            return ProjectionEditResult(ProjectionEditDisposition.EDITED, message)


projection_editor = TelegramProjectionEditor()


def install_runtime_projection_editing(server) -> None:
    """Wrap the existing input acknowledgement edit path in IR-9 fencing."""

    if getattr(server, "_ir9_projection_editing_installed", False):
        return
    base_apply = server.apply_input_ack_policy

    async def fenced_apply_input_ack_policy(*, update, submission, session_id):
        policy = str(submission.get("ack_policy") or "silent")
        ref = dict(submission.get("presentation_ref") or {})
        message_id = ref.get("client_message_id")
        if policy != "update_existing" or message_id is None:
            return await base_apply(
                update=update,
                submission=submission,
                session_id=session_id,
            )

        generation = server.session_generations.current(session_id)
        presentation_id = str(
            ref.get("presentation_id")
            or submission.get("input_batch_id")
            or f"message:{message_id}"
        )
        # ``presentation_generation`` belongs to relocation/handle identity and
        # may legitimately stay constant across QUEUED -> APPLYING -> APPLIED.
        # A dedicated optional projection revision may be supplied by a future
        # presentation producer; otherwise this editor allocates one locally.
        revision_value = ref.get("projection_revision")
        try:
            revision = int(revision_value) if revision_value is not None else None
        except (TypeError, ValueError):
            revision = None
        text = server._presentation_text(submission)

        async def edit():
            await server.application.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=int(message_id),
                text=text,
            )
            return SimpleNamespace(message_id=int(message_id))

        async def send_new():
            return await server.telegram_reply_with_retries(
                update,
                text,
                parse_mode=None,
                max_retries=1,
                base_delay=0.0,
            )

        result = await projection_editor.update(
            session_id=session_id,
            presentation_id=presentation_id,
            session_generation=generation,
            revision=revision,
            edit=edit,
            send_new=send_new,
            generation_is_current=(
                lambda: server.session_generations.current(session_id) == generation
            ),
        )
        if result.disposition == ProjectionEditDisposition.FALLBACK_SENT:
            return result.message
        if result.message is not None:
            return result.message
        return SimpleNamespace(message_id=int(message_id))

    server.apply_input_ack_policy = fenced_apply_input_ack_policy
    server._ir9_projection_editing_installed = True
