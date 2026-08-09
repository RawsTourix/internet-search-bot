"""IR-9 Telegram presentation-only ordering and edit fallback.

This layer fences visual writes only. It never mutates durable input/runtime
authority and never creates AgentEmission/OutputBatch records.
"""

from __future__ import annotations

import asyncio
import hmac
import json
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


async def apply_applied_addendum_projection(
    *,
    server: Any,
    gateway: Any,
    payload: dict[str, Any],
    editor: TelegramProjectionEditor = projection_editor,
) -> dict[str, Any]:
    """Edit the exact addendum presentation selected by structured InputBatch ID."""

    event = dict(payload.get("event") or {})
    target = dict(payload.get("target") or {})
    data = dict(event.get("data") or {})
    session_id = str(target.get("session_id") or "").strip()
    target_generation = target.get("session_generation")
    input_batch_id = str(data.get("input_batch_id") or "").strip()
    if not session_id or target_generation is None or not input_batch_id:
        return {"status": "ignored", "reason": "missing projection target"}
    try:
        session_generation = int(target_generation)
    except (TypeError, ValueError):
        return {"status": "ignored", "reason": "invalid session generation"}
    if not server.session_generations.is_current(
        session_id,
        session_generation,
    ):
        return {"status": "ignored", "reason": "stale session generation"}

    lookup = getattr(gateway, "runtime_input_presentation", None)
    if not callable(lookup):
        return {"status": "ignored", "reason": "presentation unavailable"}
    presentation = await lookup(input_batch_id)
    if not presentation:
        return {"status": "ignored", "reason": "presentation unavailable"}
    presentation_id = str(presentation.get("presentation_id") or "").strip()
    message_id = presentation.get("message_id")
    chat_id = target.get("chat_id") or target.get("conversation_id")
    if not presentation_id or message_id is None or chat_id is None:
        return {"status": "ignored", "reason": "presentation unavailable"}

    locale = server.normalize_locale(data.get("locale"))
    text = server._localized(
        "input_runtime.addendum.applied",
        locale=locale,
    )

    async def edit():
        await server.application.bot.edit_message_text(
            chat_id=int(chat_id),
            message_id=int(message_id),
            text=text,
        )
        return SimpleNamespace(message_id=int(message_id))

    async def send_new():
        return await server.application.bot.send_message(
            chat_id=int(chat_id),
            text=text,
        )

    result = await editor.update(
        session_id=session_id,
        presentation_id=presentation_id,
        session_generation=session_generation,
        edit=edit,
        send_new=send_new,
        generation_is_current=(
            lambda: server.session_generations.is_current(
                session_id,
                session_generation,
            )
        ),
    )
    if (
        result.disposition == ProjectionEditDisposition.FALLBACK_SENT
        and result.message is not None
    ):
        replace = getattr(
            gateway,
            "replace_runtime_input_presentation_message_id",
            None,
        )
        if callable(replace):
            await replace(
                input_batch_id,
                expected_presentation_id=presentation_id,
                message_id=result.message.message_id,
            )
    return {
        "status": "handled",
        "disposition": result.disposition.value,
    }


class _RuntimeProjectionProgressMiddleware:
    """Intercept only IR-9 structured addendum projections before generic progress."""

    def __init__(self, app: Any, *, server: Any, gateway: Any) -> None:
        self.app = app
        self.server = server
        self.gateway = gateway

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or scope.get("path") != "/internal/progress":
            await self.app(scope, receive, send)
            return

        messages: list[dict[str, Any]] = []
        body = bytearray()
        while True:
            message = await receive()
            messages.append(message)
            if message.get("type") != "http.request":
                continue
            body.extend(message.get("body") or b"")
            if not message.get("more_body", False):
                break

        async def replay_receive():
            if messages:
                return messages.pop(0)
            return {"type": "http.request", "body": b"", "more_body": False}

        try:
            payload = json.loads(bytes(body).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            await self.app(scope, replay_receive, send)
            return
        if not isinstance(payload, dict):
            await self.app(scope, replay_receive, send)
            return
        event = payload.get("event") or {}
        if (
            payload.get("client_type") != "telegram"
            or not isinstance(event, dict)
            or event.get("type") != "input_addendum_applied"
            or event.get("visibility", "user") != "user"
        ):
            await self.app(scope, replay_receive, send)
            return

        configured_token = str(
            getattr(self.server, "TELEGRAM_PROGRESS_CALLBACK_TOKEN", "") or ""
        )
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        supplied_token = headers.get("x-progress-token", "")
        if configured_token and not hmac.compare_digest(
            supplied_token,
            configured_token,
        ):
            # Preserve the canonical handler's existing 401 response.
            await self.app(scope, replay_receive, send)
            return

        result = await apply_applied_addendum_projection(
            server=self.server,
            gateway=self.gateway,
            payload=payload,
        )
        response_body = json.dumps(
            result,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(response_body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": response_body})


def install_runtime_projection_progress_middleware(server: Any, gateway: Any) -> None:
    """Install one presentation-only interception layer before FastAPI startup."""

    if getattr(server, "_ir9_projection_progress_middleware_installed", False):
        return
    app = getattr(server, "app", None)
    if app is None or not hasattr(app, "add_middleware"):
        return
    app.add_middleware(
        _RuntimeProjectionProgressMiddleware,
        server=server,
        gateway=gateway,
    )
    server._ir9_projection_progress_middleware_installed = True
