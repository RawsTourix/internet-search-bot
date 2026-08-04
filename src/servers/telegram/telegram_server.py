import asyncio
import hmac
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Callable
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from telegram import BotCommand, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from ...adapters.telegram_resolvers import TelegramInputResolverRegistry
from ...interaction.config import LocalizationConfigType
from ...interaction.output_models import OutputBatch, OutputDeliveryPlan
from ...localization.models import LocalizationMessage
from ...localization.service import LocalizationService

from .artifact_bridge import (
    TelegramArtifactBridgeError,
    TelegramArtifactGatewayClient,
    build_telegram_input_envelope,
    extract_telegram_attachments,
    telegram_media_group_key,
    telegram_session_id,
)
from .config import (
    BOT_TOKEN,
    GATEWAY_URL,
    PROGRESS_EDIT_MIN_INTERVAL,
    PROGRESS_MAX_TEXT_LENGTH,
    TELEGRAM_API_KEY,
    TELEGRAM_BOT_INSTANCE_ID,
    TELEGRAM_DELIVERY_SPOOL_MEMORY_BYTES,
    TELEGRAM_FILE_PROVIDER_TOKEN,
    TELEGRAM_FINAL_DELIVERY_MODE,
    TELEGRAM_FINAL_EDIT_MAX_LENGTH,
    TELEGRAM_MEDIA_GROUP_MAX_LIFETIME_SECONDS,
    TELEGRAM_MEDIA_GROUP_QUIET_PERIOD_SECONDS,
    TELEGRAM_PROGRESS_CALLBACK_TOKEN,
    TELEGRAM_PROGRESS_CALLBACK_URL,
    WEBHOOK_DOMAIN,
    WEBHOOK_SECRET,
)
from .media_group_runner import (
    LifetimeBoundDebouncedBatchRunner,
    LifetimeMediaGroupActivityCoordinator,
)
from .output_plan_executor import (
    TelegramExecutionContext,
    TelegramOutputPlanExecutor,
)


telegram_input_resolvers = TelegramInputResolverRegistry()
telegram_output_executor = TelegramOutputPlanExecutor()
telegram_localization = LocalizationService.from_directory(
    config=LocalizationConfigType()
)
from .runtime_state import KeyedAsyncLockPool, SessionGenerationRegistry
from ...utils.telegram_formatting import (
    markdown_to_plain_text,
    markdown_to_telegram_html,
    split_markdown_for_telegram,
)


log_dir = "logging"
os.makedirs(log_dir, exist_ok=True)
formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("TelegramServer")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    file_handler = RotatingFileHandler(
        filename=os.path.join(log_dir, "telegram_server.log"),
        maxBytes=8 * 1024 * 1024,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)


application = Application.builder().token(BOT_TOKEN).build()
media_group_activity = LifetimeMediaGroupActivityCoordinator()
artifact_gateway = TelegramArtifactGatewayClient(
    gateway_url=GATEWAY_URL,
    api_key=TELEGRAM_API_KEY,
    delivery_spool_memory_bytes=TELEGRAM_DELIVERY_SPOOL_MEMORY_BYTES,
    media_group_activity=media_group_activity,
)
media_group_runner = LifetimeBoundDebouncedBatchRunner(
    activity=media_group_activity,
    maximum_lifetime_seconds=TELEGRAM_MEDIA_GROUP_MAX_LIFETIME_SECONDS,
)
standalone_lock_pool = KeyedAsyncLockPool()

progress_edit_state: dict[str, dict[str, Any]] = {}
progress_edit_queues: dict[str, asyncio.Queue[tuple[int, str]]] = {}
progress_edit_workers: dict[str, asyncio.Task[None]] = {}
progress_edit_versions: dict[str, int] = {}
media_groups: dict[str, "PendingMediaGroup"] = {}
media_groups_guard = asyncio.Lock()
session_generations = SessionGenerationRegistry()


@dataclass(slots=True)
class PendingMediaGroup:
    key: str
    input_batch_id: str | None
    session_id: str
    progress_locale: str
    update: Update
    status_message: Any
    response_metadata: dict[str, Any]
    generation: int = 0
    failed: bool = False
    terminal_notified: bool = False


def detect_progress_locale(update: Update) -> str:
    language_code = getattr(update.effective_user, "language_code", None)
    return "en" if language_code and language_code.lower().startswith("en") else "ru"


def normalize_locale(value: str | None) -> str:
    return "en" if (value or "ru").lower().strip().startswith("en") else "ru"


def is_agent_error(metadata: dict[str, Any]) -> bool:
    status_value = str(metadata.get("agent_status") or "").lower()
    return status_value in {"error", "agentstatus.error"} or bool(
        metadata.get("error")
    )


def extract_error_type_summary(error_text: str) -> str:
    text = error_text or ""
    if "ConnectError" in text:
        return "LLMTransportError / ConnectError"
    if "Timeout" in text or "таймаут" in text.lower():
        return "LLMTimeoutError"
    match = re.search(r"\bHTTP\s*(\d{3})\b", text, flags=re.IGNORECASE)
    if not match:
        match = re.search(
            r"\bLLM\s+API\s*:\s*(\d{3})\b",
            text,
            flags=re.IGNORECASE,
        )
    if not match:
        match = re.search(
            r"status[_ ]code[=:\s]+(\d{3})",
            text,
            flags=re.IGNORECASE,
        )
    if match:
        return f"LLMHTTPError / HTTP {match.group(1)}"
    if "LLMTransportError" in text:
        return "LLMTransportError"
    if "LLMHTTPError" in text:
        return "LLMHTTPError"
    return "RuntimeError"


def extract_llm_http_status(error_type: str) -> int | None:
    match = re.search(r"\bHTTP\s+(\d{3})\b", error_type, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def format_agent_error_for_telegram(
    message: str,
    metadata: dict[str, Any],
    *,
    locale_name: str = "ru",
) -> str:
    locale_name = normalize_locale(locale_name)
    error_message = str(metadata.get("error") or message or "")
    iterations = metadata.get("iterations") or "?"
    error_kind = metadata.get("error_kind")
    error_type = extract_error_type_summary(error_message)
    http_status = extract_llm_http_status(error_type)
    if error_kind == "context_limit_interruption":
        key = "error.context_limit"
    elif error_kind == "llm_configuration_error" or (
        error_kind != "infrastructure_interruption"
        and http_status in {400, 401, 403, 404, 422}
    ):
        key = "error.llm_configuration"
    elif (
        error_kind == "infrastructure_interruption"
        or "LLMTransportError" in error_type
        or "LLMTimeoutError" in error_type
        or http_status in {429, 500, 502, 503, 504}
    ):
        key = "error.infrastructure_interruption"
    else:
        key = "error.agent"
    return _localized(
        key,
        locale=locale_name,
        error_type=error_type,
        iteration=iterations,
        error_message=error_message,
    )


def _safe_transport_error(
    error: BaseException,
    *,
    locale: str = "ru",
    input_kind: str = "file",
) -> str:
    if input_kind == "message":
        return _localized("error.input_message", locale=locale)
    if isinstance(error, httpx.HTTPStatusError):
        return _localized(
            "error.transport_http",
            locale=locale,
            status_code=error.response.status_code,
        )
    if isinstance(error, (httpx.TimeoutException, TimedOut)):
        return _localized("error.transport_timeout", locale=locale)
    if isinstance(error, (httpx.RequestError, NetworkError)):
        return _localized("error.transport_unavailable", locale=locale)
    if isinstance(error, TelegramArtifactBridgeError):
        return _localized("error.unsafe_file", locale=locale)
    return _localized("error.internal", locale=locale)


async def _claim_explicit_failure_notification(session_id: str) -> bool:
    claim = getattr(
        artifact_gateway,
        "claim_explicit_ingress_failure",
        None,
    )
    if not callable(claim):
        return True
    return bool(await claim(session_id))


async def send_to_gateway(payload: dict) -> tuple[bool, str, dict[str, Any]]:
    try:
        timeout = httpx.Timeout(
            connect=10.0,
            read=1800.0,
            write=30.0,
            pool=10.0,
        )
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{GATEWAY_URL}/message",
                json=payload,
                headers={"X-API-Key": TELEGRAM_API_KEY},
            )
            response.raise_for_status()
            data = response.json()
            return (
                True,
                data.get(
                    "response",
                    _localized("gateway.request_accepted", locale="ru"),
                ),
                data.get("metadata", {}) or {},
            )
    except Exception as error:
        logger.exception("Gateway text request failed: %r", error)
        return False, _safe_transport_error(error), {}


async def send_initial_status_message(update: Update, text: str):
    try:
        return await update.effective_message.reply_text(text)
    except (TimedOut, NetworkError) as error:
        logger.warning("Failed to send initial Telegram status: %r", error)
        return None


async def bind_input_presentation_status(
    *,
    submission: dict[str, Any],
    status_message: Any,
    session_id: str,
) -> None:
    """Best-effort binding; durable input admission must not depend on presentation."""
    if status_message is None:
        return
    try:
        await artifact_gateway.bind_input_presentation(
            submission.get("presentation_ref"),
            session_id=session_id,
            client_message_id=str(status_message.message_id),
        )
    except Exception as error:
        logger.warning(
            "Failed to bind InputBatch presentation status: %s",
            type(error).__name__,
        )


def _presentation_text(submission: dict[str, Any]) -> str:
    event = submission.get("presentation_event") or {}
    key = str(event.get("message_key") or "")
    params = event.get("params") or {}
    locale = normalize_locale(event.get("locale"))
    if not key:
        key = "input_batch.updated"
    return _localized(
        key,
        locale=locale,
        **params,
    )


async def apply_input_ack_policy(
    *,
    update: Update,
    submission: dict[str, Any],
    session_id: str,
) -> Any | None:
    """Execute the structured ingress acknowledgement without creating spam."""
    policy = str(submission.get("ack_policy") or "silent")
    ref = submission.get("presentation_ref") or {}
    text = _presentation_text(submission)
    if policy == "create":
        status_message = await send_initial_status_message(update, text)
        await bind_input_presentation_status(
            submission=submission,
            status_message=status_message,
            session_id=session_id,
        )
        return status_message
    message_id = ref.get("client_message_id")
    if policy == "update_existing" and message_id is not None:
        try:
            await application.bot.edit_message_text(
                chat_id=update.effective_chat.id,
                message_id=int(message_id),
                text=text,
            )
        except (BadRequest, TimedOut, NetworkError) as error:
            logger.warning(
                "Input presentation update failed: %s",
                type(error).__name__,
            )
        return SimpleNamespace(message_id=int(message_id))
    if policy == "throttled_update" and message_id is not None:
        enqueue_progress_message(
            chat_id=update.effective_chat.id,
            message_id=int(message_id),
            text=text,
        )
        return SimpleNamespace(message_id=int(message_id))
    if message_id is not None:
        return SimpleNamespace(message_id=int(message_id))
    return None


def _progress_metadata(
    update: Update,
    status_message: Any,
    *,
    request_id: str | None = None,
) -> dict[str, Any]:
    chat = getattr(update, "effective_chat", None)
    message = getattr(update, "effective_message", None)
    chat_id = getattr(chat, "id", None)
    message_id = getattr(message, "message_id", None)
    metadata: dict[str, Any] = {
        "progress_locale": detect_progress_locale(update),
    }
    if chat_id is not None:
        metadata["chat_id"] = chat_id
    if message_id is not None:
        metadata["message_id"] = message_id
    if status_message is not None:
        status_message_id = getattr(status_message, "message_id", None)
        progress_request_id = request_id
        if progress_request_id is None:
            update_id = getattr(update, "update_id", None)
            if update_id is not None:
                progress_request_id = str(update_id)
        if status_message_id is not None:
            metadata["status_message_id"] = status_message_id
        if progress_request_id is not None:
            metadata["progress_request_id"] = str(progress_request_id)
        if chat_id is not None and status_message_id is not None:
            progress_target = {
                "chat_id": chat_id,
                "message_id": status_message_id,
            }
            # Some non-message updates and compatibility callers do not expose
            # ``effective_message``.  Preserve the historical target shape for
            # those callers; generation fencing is available for real Telegram
            # message updates where a stable session can be derived.
            if message is not None:
                session_id = _session_for_update(update)
                progress_target.update(
                    session_id=session_id,
                    session_generation=session_generations.current(session_id),
                )
            metadata["progress_target"] = progress_target
    if TELEGRAM_PROGRESS_CALLBACK_URL:
        metadata["progress_callback_url"] = TELEGRAM_PROGRESS_CALLBACK_URL
    if TELEGRAM_PROGRESS_CALLBACK_TOKEN:
        metadata["progress_callback_token"] = TELEGRAM_PROGRESS_CALLBACK_TOKEN
    return metadata


def attach_progress_metadata(
    *,
    payload: dict,
    update: Update,
    status_message: Any,
) -> None:
    payload.setdefault("metadata", {}).update(
        _progress_metadata(
            update,
            status_message,
            request_id=str(payload.get("id")) if payload.get("id") is not None else None,
        )
    )


async def telegram_reply_with_retries(
    update: Update,
    text: str,
    *,
    parse_mode=None,
    disable_web_page_preview: bool = True,
    max_retries: int = 5,
    base_delay: float = 2.0,
):
    last_error: BaseException | None = None
    for attempt in range(1, max_retries + 1):
        try:
            return await update.effective_message.reply_text(
                text,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
            )
        except BadRequest:
            raise
        except (TimedOut, NetworkError) as error:
            last_error = error
            if attempt >= max_retries:
                break
            await asyncio.sleep(base_delay * (2 ** (attempt - 1)))
    if last_error is not None:
        raise last_error


async def send_telegram_markdown_reply(update: Update, text: str):
    for markdown_chunk in split_markdown_for_telegram(text or ""):
        try:
            await telegram_reply_with_retries(
                update,
                markdown_to_telegram_html(markdown_chunk),
                parse_mode=ParseMode.HTML,
            )
        except BadRequest:
            await telegram_reply_with_retries(
                update,
                markdown_to_plain_text(markdown_chunk),
                parse_mode=None,
            )
        except (TimedOut, NetworkError) as error:
            logger.error("Telegram text delivery failed: %r", error)
            break


async def edit_telegram_message_with_retries(
    *,
    chat_id: int,
    message_id: int,
    text: str,
    parse_mode=None,
    disable_web_page_preview: bool = True,
    max_retries: int = 5,
    base_delay: float = 2.0,
    is_stale: Callable[[], bool] | None = None,
):
    last_error: BaseException | None = None
    for attempt in range(1, max_retries + 1):
        if is_stale is not None and is_stale():
            return None
        try:
            return await application.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
            )
        except BadRequest as error:
            if "message is not modified" in str(error).lower():
                return None
            raise
        except (TimedOut, NetworkError) as error:
            last_error = error
            if is_stale is not None and is_stale():
                return None
            if attempt >= max_retries:
                break
            await asyncio.sleep(base_delay * (2 ** (attempt - 1)))
    if last_error is not None:
        if is_stale is not None and is_stale():
            return None
        raise last_error


async def maybe_edit_progress_message(
    *,
    chat_id: int,
    message_id: int,
    text: str,
    version: int | None = None,
) -> None:
    text = (text or "").strip()
    if not text:
        return
    if len(text) > PROGRESS_MAX_TEXT_LENGTH:
        text = text[:PROGRESS_MAX_TEXT_LENGTH] + "…"
    key = f"{chat_id}:{message_id}"
    is_stale = (
        (lambda: progress_edit_versions.get(key) != version)
        if version is not None
        else None
    )
    if is_stale is not None and is_stale():
        return
    now = asyncio.get_running_loop().time()
    state = progress_edit_state.get(key) or {}
    if state.get("last_text") == text:
        return
    wait = PROGRESS_EDIT_MIN_INTERVAL - (
        now - float(state.get("last_edit_at", 0.0))
    )
    if wait > 0:
        await asyncio.sleep(wait)
        if is_stale is not None and is_stale():
            return
        now = asyncio.get_running_loop().time()
    try:
        await edit_telegram_message_with_retries(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=None,
            is_stale=is_stale,
        )
        if is_stale is None or not is_stale():
            progress_edit_state[key] = {
                "last_text": text,
                "last_edit_at": now,
            }
    except Exception as error:
        logger.warning("Failed to edit progress message: %r", error)


async def _run_progress_edit_worker(
    *,
    key: str,
    chat_id: int,
    message_id: int,
    queue: asyncio.Queue[tuple[int, str]],
) -> None:
    try:
        while True:
            pending = [await queue.get()]
            while True:
                try:
                    pending.append(queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            for _ in pending[:-1]:
                queue.task_done()
            version, text = pending[-1]
            try:
                await maybe_edit_progress_message(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    version=version,
                )
            finally:
                queue.task_done()
            if queue.empty():
                break
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Progress edit worker failed")
    finally:
        current = asyncio.current_task()
        if progress_edit_workers.get(key) is current:
            progress_edit_workers.pop(key, None)
        if queue.empty() and progress_edit_queues.get(key) is queue:
            progress_edit_queues.pop(key, None)
            progress_edit_versions.pop(key, None)


def enqueue_progress_message(
    *,
    chat_id: int,
    message_id: int,
    text: str,
) -> int:
    key = f"{chat_id}:{message_id}"
    version = progress_edit_versions.get(key, 0) + 1
    progress_edit_versions[key] = version
    queue = progress_edit_queues.setdefault(key, asyncio.Queue())
    queue.put_nowait((version, text))
    worker = progress_edit_workers.get(key)
    if worker is None or worker.done():
        progress_edit_workers[key] = asyncio.create_task(
            _run_progress_edit_worker(
                key=key,
                chat_id=chat_id,
                message_id=message_id,
                queue=queue,
            )
        )
    return version


async def stop_progress_edits(*, chat_id: int, message_id: int) -> None:
    key = f"{chat_id}:{message_id}"
    progress_edit_versions[key] = progress_edit_versions.get(key, 0) + 1
    worker = progress_edit_workers.pop(key, None)
    if worker is not None and not worker.done():
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
    queue = progress_edit_queues.pop(key, None)
    if queue is not None:
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                queue.task_done()
    progress_edit_state.pop(key, None)
    progress_edit_versions.pop(key, None)


async def stop_all_progress_edits() -> None:
    for key in set(progress_edit_workers) | set(progress_edit_queues):
        chat_id, message_id = key.split(":", maxsplit=1)
        await stop_progress_edits(
            chat_id=int(chat_id),
            message_id=int(message_id),
        )


async def finish_status_or_send_reply(
    *,
    update: Update,
    status_message: Any,
    text: str,
    force_reply_if_long: bool = False,
    delivery_mode: str | None = None,
) -> Any:
    mode = (delivery_mode or TELEGRAM_FINAL_DELIVERY_MODE).lower().strip()
    if mode not in {"send_new", "edit_status", "auto"}:
        mode = "send_new"
    if status_message is None:
        return await send_telegram_markdown_reply(update, text)
    await stop_progress_edits(
        chat_id=update.effective_chat.id,
        message_id=status_message.message_id,
    )
    raw = text or ""
    chunks = split_markdown_for_telegram(raw)
    if (
        mode == "send_new"
        or force_reply_if_long
        or len(raw) > TELEGRAM_FINAL_EDIT_MAX_LENGTH
        or len(chunks) != 1
    ):
        return await send_telegram_markdown_reply(update, raw)
    chunk = chunks[0]
    try:
        await edit_telegram_message_with_retries(
            chat_id=update.effective_chat.id,
            message_id=status_message.message_id,
            text=markdown_to_telegram_html(chunk),
            parse_mode=ParseMode.HTML,
        )
        return status_message
    except BadRequest:
        try:
            await edit_telegram_message_with_retries(
                chat_id=update.effective_chat.id,
                message_id=status_message.message_id,
                text=markdown_to_plain_text(chunk),
                parse_mode=None,
            )
            return status_message
        except (TimedOut, NetworkError):
            return await send_telegram_markdown_reply(update, raw)
    except (TimedOut, NetworkError):
        return await send_telegram_markdown_reply(update, raw)


async def _deliver_agent_result(
    *,
    update: Update,
    status_message: Any,
    success: bool,
    message: str,
    metadata: dict[str, Any],
    session_id: str,
) -> None:
    response_generation = metadata.get("telegram_session_generation")
    if (
        response_generation is not None
        and not session_generations.is_current(
            session_id,
            int(response_generation),
        )
    ):
        logger.info(
            "telegram_terminal_delivery_stale session_id=%s generation=%s",
            session_id,
            response_generation,
        )
        return
    locale_name = normalize_locale(
        metadata.get("progress_locale") or detect_progress_locale(update)
    )
    output_batch = metadata.get("output_batch") or {}
    anchor = output_batch.get("response_anchor") or {}
    try:
        reply_message_id = int(anchor.get("client_message_id"))
    except (TypeError, ValueError):
        reply_message_id = update.effective_message.message_id
    output_batch_id = str(output_batch.get("output_batch_id") or "")
    if success and not is_agent_error(metadata):
        if not output_batch_id:
            await finish_status_or_send_reply(
                update=update,
                status_message=status_message,
                text=(message or "").strip() or _localized(
                    "output.done",
                    locale=locale_name,
                ),
                delivery_mode=TELEGRAM_FINAL_DELIVERY_MODE,
            )
            return
        claim = await artifact_gateway.claim_output_batch(
            output_batch_id,
            session_id=session_id,
        )
        batch = OutputBatch.model_validate(claim.get("output_batch"))
        plan = OutputDeliveryPlan.model_validate(claim.get("delivery_plan"))
        attempt_id = str(claim.get("attempt_id") or "")
        receipt = await telegram_output_executor.execute(
            batch=batch,
            plan=plan,
            attempt_id=attempt_id,
            context=TelegramExecutionContext(
                bot=application.bot,
                gateway=artifact_gateway,
                session_id=session_id,
                chat_id=update.effective_chat.id,
                message_thread_id=getattr(
                    update.effective_message,
                    "message_thread_id",
                    None,
                ),
                reply_to_message_id=reply_message_id,
                status_message_id=getattr(
                    status_message,
                    "message_id",
                    None,
                ),
            ),
        )
        try:
            completed = await artifact_gateway.complete_output_batch(
                output_batch_id,
                session_id=session_id,
                receipt=receipt.model_dump(mode="json"),
            )
        except Exception:
            logger.exception(
                "OutputBatch receipt persistence failed: %s",
                output_batch_id,
            )
            await finish_status_or_send_reply(
                update=update,
                status_message=status_message,
                text=_localized(
                    "output.receipt_persistence_failed",
                    locale=locale_name,
                ),
                delivery_mode=TELEGRAM_FINAL_DELIVERY_MODE,
            )
            return
        terminal_state = str(completed.get("state") or receipt.state.value)
        key = {
            "delivered": "output.done",
            "partially_delivered": "output.delivery_incomplete",
            "failed": "output_batch.failed",
            "unknown": "output.delivery_unknown",
        }.get(terminal_state, "output.delivery_unknown")
        await finish_status_or_send_reply(
            update=update,
            status_message=status_message,
            text=_localized(key, locale=locale_name),
            delivery_mode=TELEGRAM_FINAL_DELIVERY_MODE,
        )
        return
    text = (
        format_agent_error_for_telegram(
            message,
            metadata,
            locale_name=locale_name,
        )
        if success
        else _localized(
            "error.request",
            locale=locale_name,
            message=message,
        )
    )
    await finish_status_or_send_reply(
        update=update,
        status_message=status_message,
        text=text,
        delivery_mode="send_new",
    )


def _localized(
    message_key: str,
    *,
    locale: str,
    **params: Any,
) -> str:
    try:
        return telegram_localization.render(
            LocalizationMessage(
                message_key=message_key,
                params=params,
            ),
            locale=locale,
        )
    except Exception:
        logger.exception(
            "Localization unavailable for Telegram key=%s",
            message_key,
        )
        return message_key


def _session_for_update(update: Update) -> str:
    return telegram_session_id(
        update.effective_chat.id,
        getattr(update.effective_message, "message_thread_id", None),
    )


async def command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    full_text = update.effective_message.text or ""
    words = full_text.split()
    normalized_command = (
        words[0].lower().split("@", 1)[0] if words else ""
    )
    payload = {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.now().isoformat(),
        "client_type": "telegram",
        "message_type": "command",
        "content": full_text,
        "user_id": str(update.effective_user.id),
        "user_name": update.effective_user.full_name,
        "metadata": {
            "chat_id": update.effective_chat.id,
            "message_id": update.effective_message.message_id,
            "session_id": _session_for_update(update),
        },
        "command": words[0] if words else "",
        "arguments": words[1:],
    }
    status_message = None
    if normalized_command not in {"/status", "/reset"}:
        status_message = await send_initial_status_message(
            update,
            _localized(
                "input.command_received",
                locale=detect_progress_locale(update),
            ),
        )
    attach_progress_metadata(
        payload=payload,
        update=update,
        status_message=status_message,
    )
    if normalized_command == "/reset":
        await reset_process_local_session(_session_for_update(update))
    success, message, metadata = await send_to_gateway(payload)
    if normalized_command == "/status" and success:
        activity = await media_group_activity.snapshot_all()
        message = (
            f"{message}\n\nTelegram transport:\n"
            f"• Активных media groups: {len(media_groups)}\n"
            f"• In-flight downloads: {activity['in_flight']}"
        )
    metadata["telegram_session_generation"] = session_generations.current(
        _session_for_update(update)
    )
    await _deliver_agent_result(
        update=update,
        status_message=status_message,
        success=success,
        message=message,
        metadata=metadata,
        session_id=_session_for_update(update),
    )


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session_id = _session_for_update(update)
    generation = session_generations.current(session_id)
    progress_locale = detect_progress_locale(update)
    try:
        semantic_parts = await telegram_input_resolvers.resolve(
            update.effective_message
        )
        envelope = build_telegram_input_envelope(
            update,
            bot_instance_id=TELEGRAM_BOT_INSTANCE_ID,
            semantic_parts=semantic_parts,
        )
        submission = await artifact_gateway.submit_envelope(
            envelope,
            progress_locale=progress_locale,
        )
        status_message = await apply_input_ack_policy(
            update=update,
            submission=submission,
            session_id=session_id,
        )
        if submission.get("status") != "committed":
            return
        if submission.get("duplicate"):
            return
        batch_id = str(submission.get("input_batch_id") or "")
        if not batch_id:
            raise TelegramArtifactBridgeError(
                "Gateway returned no input batch ID"
            )
        payload = await artifact_gateway.run_committed(
            batch_id,
            session_id=session_id,
            progress_locale=progress_locale,
        )
        metadata = payload.get("metadata", {}) or {}
        metadata.setdefault("progress_locale", progress_locale)
        metadata["telegram_session_generation"] = generation
        await _deliver_agent_result(
            update=update,
            status_message=status_message,
            success=True,
            message=str(payload.get("response") or ""),
            metadata=metadata,
            session_id=session_id,
        )
    except Exception as error:
        logger.exception("Telegram semantic text input failed: %r", error)
        if not await _claim_explicit_failure_notification(session_id):
            logger.info(
                "telegram_collection_cascade_error_suppressed "
                "session_id=%s input_kind=message",
                session_id,
            )
            return
        await _deliver_agent_result(
            update=update,
            status_message=None,
            success=False,
            message=_safe_transport_error(
                error,
                locale=progress_locale,
                input_kind="message",
            ),
            metadata={
                "progress_locale": progress_locale,
                "telegram_session_generation": generation,
            },
            session_id=session_id,
        )


async def _claim_group_failure(group: PendingMediaGroup) -> bool:
    async with media_groups_guard:
        if group.terminal_notified or media_groups.get(group.key) is not group:
            return False
        group.failed = True
        group.terminal_notified = True
        media_groups.pop(group.key, None)
        return True


async def _expire_group(group: PendingMediaGroup) -> None:
    if not await _claim_group_failure(group):
        return
    logger.error(
        "telegram_media_group_lifetime_exceeded group_key=%s "
        "input_batch_id=%s maximum_lifetime_seconds=%s",
        group.key,
        group.input_batch_id,
        TELEGRAM_MEDIA_GROUP_MAX_LIFETIME_SECONDS,
    )
    text = _localized(
        "input.album_timeout",
        locale=group.progress_locale,
        timeout_seconds=f"{TELEGRAM_MEDIA_GROUP_MAX_LIFETIME_SECONDS:g}",
    )
    await finish_status_or_send_reply(
        update=group.update,
        status_message=group.status_message,
        text=text,
        delivery_mode="send_new",
    )


async def _finish_group(group: PendingMediaGroup) -> None:
    try:
        if not session_generations.is_current(
            group.session_id,
            group.generation,
        ):
            logger.info(
                "telegram_media_group_callback_stale session_id=%s group_key=%s",
                group.session_id,
                group.key,
            )
            return
        if group.failed or group.input_batch_id is None:
            return
        payload = await artifact_gateway.commit_and_run(
            group.input_batch_id,
            session_id=group.session_id,
            progress_locale=group.progress_locale,
        )
        if payload.get("run_skipped_duplicate"):
            await finish_status_or_send_reply(
                update=group.update,
                status_message=group.status_message,
                text=_localized(
                    "input.album_duplicate",
                    locale=group.progress_locale,
                ),
                delivery_mode="send_new",
            )
            return
        metadata = payload.get("metadata", {}) or {}
        metadata.setdefault("progress_locale", group.progress_locale)
        metadata["telegram_session_generation"] = group.generation
        await _deliver_agent_result(
            update=group.update,
            status_message=group.status_message,
            success=True,
            message=str(payload.get("response") or ""),
            metadata=metadata,
            session_id=group.session_id,
        )
    except Exception as error:
        logger.exception("Media-group processing failed: %r", error)
        await _deliver_agent_result(
            update=group.update,
            status_message=group.status_message,
            success=False,
            message=_safe_transport_error(
                error,
                locale=group.progress_locale,
            ),
            metadata={
                "progress_locale": group.progress_locale,
                "telegram_session_generation": group.generation,
            },
            session_id=group.session_id,
        )
    finally:
        async with media_groups_guard:
            if media_groups.get(group.key) is group:
                media_groups.pop(group.key, None)


async def _process_standalone_attachment(
    update: Update,
    *,
    semantic_parts: list[Any] | None = None,
) -> None:
    progress_locale = detect_progress_locale(update)
    session_id = _session_for_update(update)
    explicit_active = False
    collection_active = getattr(
        artifact_gateway,
        "is_explicit_collection_active",
        None,
    )
    if callable(collection_active):
        explicit_active = await collection_active(session_id)
    status_message = None
    if not explicit_active:
        status_message = await send_initial_status_message(
            update,
            _localized(
                "input.file_received",
                locale=progress_locale,
            ),
        )
    generation = session_generations.current(session_id)
    try:
        if semantic_parts is None:
            semantic_parts = await telegram_input_resolvers.resolve(
                update.effective_message,
            )
        envelope = build_telegram_input_envelope(
            update,
            bot_instance_id=TELEGRAM_BOT_INSTANCE_ID,
            response_metadata=_progress_metadata(update, status_message),
            semantic_parts=semantic_parts,
        )
        if envelope.attachment_slots:
            envelope = envelope.model_copy(update={
                "semantic_parts": await telegram_input_resolvers.resolve(
                    update.effective_message,
                    attachment_slots=tuple(envelope.attachment_slots),
                )
            })
        submission = await artifact_gateway.submit_envelope(
            envelope,
            progress_locale=progress_locale,
        )
        await bind_input_presentation_status(
            submission=submission,
            status_message=status_message,
            session_id=session_id,
        )
        if submission.get("status") == "failed":
            raise TelegramArtifactBridgeError("Ingress rejected the attachment")
        if submission.get("duplicate") and submission.get("status") == "committed":
            await finish_status_or_send_reply(
                update=update,
                status_message=status_message,
                text=_localized(
                    "input.duplicate",
                    locale=progress_locale,
                ),
                delivery_mode="send_new",
            )
            return
        batch_id = str(submission.get("input_batch_id") or "")
        if not batch_id:
            raise TelegramArtifactBridgeError("Gateway returned no input batch ID")
        payload = await artifact_gateway.commit_and_run(
            batch_id,
            session_id=session_id,
            progress_locale=progress_locale,
        )
        if payload.get("run_skipped_duplicate"):
            await finish_status_or_send_reply(
                update=update,
                status_message=status_message,
                text=_localized(
                    "input.duplicate",
                    locale=progress_locale,
                ),
                delivery_mode="send_new",
            )
            return
        metadata = payload.get("metadata", {}) or {}
        metadata.setdefault("progress_locale", progress_locale)
        metadata["telegram_session_generation"] = generation
        await _deliver_agent_result(
            update=update,
            status_message=status_message,
            success=True,
            message=str(payload.get("response") or ""),
            metadata=metadata,
            session_id=session_id,
        )
    except Exception as error:
        logger.exception("Standalone Telegram attachment failed: %r", error)
        if not await _claim_explicit_failure_notification(session_id):
            logger.info(
                "telegram_collection_cascade_error_suppressed "
                "session_id=%s input_kind=file",
                session_id,
            )
            return
        await _deliver_agent_result(
            update=update,
            status_message=status_message,
            success=False,
            message=_safe_transport_error(error, locale=progress_locale),
            metadata={
                "progress_locale": progress_locale,
                "telegram_session_generation": generation,
            },
            session_id=session_id,
        )


async def attachment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    attachments = extract_telegram_attachments(message)
    semantic_parts: list[Any] | None = None
    if not attachments:
        semantic_parts = await telegram_input_resolvers.resolve(message)
        if not semantic_parts:
            await message.reply_text(
                _localized(
                    "input.unsupported_type",
                    locale=detect_progress_locale(update),
                )
            )
            return
    preliminary = build_telegram_input_envelope(
        update,
        bot_instance_id=TELEGRAM_BOT_INSTANCE_ID,
        semantic_parts=semantic_parts,
    )
    if preliminary.attachment_slots:
        semantic_parts = await telegram_input_resolvers.resolve(
            message,
            attachment_slots=tuple(preliminary.attachment_slots),
        )
        preliminary = preliminary.model_copy(
            update={"semantic_parts": semantic_parts}
        )
    prepare_envelope = getattr(
        artifact_gateway,
        "prepare_input_envelope",
        None,
    )
    if callable(prepare_envelope):
        preliminary = await prepare_envelope(preliminary)
    effective_group_key = telegram_media_group_key(preliminary)
    if effective_group_key is None:
        key = (
            f"{TELEGRAM_BOT_INSTANCE_ID}:{update.update_id}:"
            f"{message.message_id}"
        )
        async with standalone_lock_pool.hold(key):
            await _process_standalone_attachment(
                update,
                semantic_parts=semantic_parts,
            )
        return

    group_key = effective_group_key
    session_id = _session_for_update(update)
    progress_locale = detect_progress_locale(update)
    created_group = False
    explicit_active = False
    collection_active = getattr(
        artifact_gateway,
        "is_explicit_collection_active",
        None,
    )
    if callable(collection_active):
        explicit_active = await collection_active(session_id)
    async with media_groups_guard:
        group = media_groups.get(group_key)
        if group is None:
            status_message = None
            if not explicit_active:
                status_message = await send_initial_status_message(
                    update,
                    _localized(
                        "input.album_collecting",
                        locale=progress_locale,
                    ),
                )
            group = PendingMediaGroup(
                key=group_key,
                input_batch_id=None,
                session_id=session_id,
                progress_locale=progress_locale,
                update=update,
                status_message=status_message,
                response_metadata=_progress_metadata(update, status_message),
                generation=session_generations.current(session_id),
            )
            media_groups[group_key] = group
            created_group = True

    try:
        envelope = build_telegram_input_envelope(
            update,
            bot_instance_id=TELEGRAM_BOT_INSTANCE_ID,
            response_metadata=group.response_metadata,
            semantic_parts=semantic_parts,
        )
        if callable(prepare_envelope):
            envelope = await prepare_envelope(envelope)
        if telegram_media_group_key(envelope) != group_key:
            raise TelegramArtifactBridgeError(
                "Telegram forwarded burst grouping changed during submission"
            )
        submission = await artifact_gateway.submit_envelope(
            envelope,
            progress_locale=progress_locale,
        )
        await bind_input_presentation_status(
            submission=submission,
            status_message=group.status_message,
            session_id=session_id,
        )
        if group.failed:
            logger.info(
                "telegram_media_group_member_ignored_after_terminal "
                "group_key=%s message_id=%s",
                group_key,
                message.message_id,
            )
            return
        if submission.get("status") == "failed":
            raise TelegramArtifactBridgeError("Ingress rejected a media-group item")
        if submission.get("status") == "committed" and submission.get("duplicate"):
            if created_group:
                async with media_groups_guard:
                    if media_groups.get(group_key) is group:
                        media_groups.pop(group_key, None)
                await finish_status_or_send_reply(
                    update=update,
                    status_message=group.status_message,
                    text=_localized(
                        "input.album_duplicate",
                        locale=progress_locale,
                    ),
                    delivery_mode="send_new",
                )
            return
        batch_id = str(submission.get("input_batch_id") or "")
        if not batch_id:
            raise TelegramArtifactBridgeError("Gateway returned no input batch ID")
        group.input_batch_id = batch_id
        scheduled = await media_group_runner.schedule(
            group_key,
            delay_seconds=TELEGRAM_MEDIA_GROUP_QUIET_PERIOD_SECONDS,
            callback=lambda: _finish_group(group),
            timeout_callback=lambda: _expire_group(group),
            reset=not bool(submission.get("duplicate")),
        )
        if not scheduled and not submission.get("duplicate"):
            logger.warning(
                "Late media-group item arrived after commit started: %s",
                group_key,
            )
    except Exception as error:
        logger.exception("Telegram media-group member failed: %r", error)
        notify = await _claim_explicit_failure_notification(group.session_id)
        if not await _claim_group_failure(group):
            return
        abort_group = getattr(media_group_runner, "abort", None)
        if callable(abort_group):
            await abort_group(group_key)
        if not notify:
            return
        await _deliver_agent_result(
            update=group.update,
            status_message=group.status_message,
            success=False,
            message=_safe_transport_error(error, locale=progress_locale),
            metadata={"progress_locale": group.progress_locale},
            session_id=group.session_id,
        )


attachment_filter = (
    filters.Document.ALL
    | filters.PHOTO
    | filters.AUDIO
    | filters.VIDEO
    | filters.VOICE
    | filters.VIDEO_NOTE
    | filters.ANIMATION
    | filters.Sticker.ALL
    | filters.LOCATION
    | filters.CONTACT
    | filters.POLL
    | filters.FORWARDED
)
application.add_handler(
    CommandHandler(["start", "status", "reset", "help"], command_handler)
)
application.add_handler(MessageHandler(attachment_filter, attachment_handler))
application.add_handler(
    MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler)
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await application.initialize()
    await application.start()
    await application.bot.set_webhook(
        url=f"{WEBHOOK_DOMAIN}/telegram/webhook",
        secret_token=WEBHOOK_SECRET,
    )
    await application.bot.set_my_commands([
        BotCommand("start", "Приветствие"),
        BotCommand("status", "Статус системы"),
        BotCommand("reset", "Очистка памяти"),
        BotCommand("help", "Справка"),
    ])
    logger.info("Telegram webhook and artifact workflows started")
    yield
    await artifact_gateway.close()
    await media_group_runner.cancel_all()
    await stop_all_progress_edits()
    await application.bot.delete_webhook()
    await application.stop()
    await application.shutdown()
    logger.info("Telegram bot stopped")


async def reset_process_local_session(session_id: str) -> None:
    """Invalidate transport-local callbacks, groups, edits and collection hints."""

    session_generations.advance(session_id)
    async with media_groups_guard:
        groups = [
            group for group in media_groups.values()
            if group.session_id == session_id
        ]
        for group in groups:
            group.failed = True
            group.terminal_notified = True
            media_groups.pop(group.key, None)
    abort_group = getattr(media_group_runner, "abort", None)
    if callable(abort_group):
        for group in groups:
            await abort_group(group.key)

    prefix = session_id.removeprefix("telegram:conversation:")
    chat_id = prefix.split(":thread:", 1)[0]
    if chat_id.lstrip("-").isdigit():
        for key in list(
            set(progress_edit_workers)
            | set(progress_edit_queues)
            | set(progress_edit_state)
        ):
            if key.startswith(f"{chat_id}:"):
                _, message_id = key.split(":", maxsplit=1)
                await stop_progress_edits(
                    chat_id=int(chat_id),
                    message_id=int(message_id),
                )

    clear_session = getattr(artifact_gateway, "clear_session_state", None)
    if callable(clear_session):
        await clear_session(session_id)


app = FastAPI(
    lifespan=lifespan,
    title="Telegram Bot Gateway",
    version="1.0.0",
)


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if not hmac.compare_digest(
        request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""),
        WEBHOOK_SECRET or "",
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )
    try:
        update = Update.de_json(await request.json(), application.bot)
        application.create_task(
            application.process_update(update),
            update=update,
        )
        return {"status": "ok"}
    except Exception as error:
        logger.exception("Telegram webhook error: %r", error)
        raise HTTPException(status_code=400, detail="Invalid Telegram update")


@app.get("/internal/files/{file_id:path}")
async def internal_file_provider(file_id: str, request: Request):
    configured = TELEGRAM_FILE_PROVIDER_TOKEN or ""
    provided = request.headers.get("X-File-Provider-Token", "")
    if not configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="File provider is not configured",
        )
    if not hmac.compare_digest(provided, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid file provider token",
        )
    try:
        stream = await artifact_gateway.open_telegram_file(
            application.bot,
            file_id,
        )
        headers = {}
        if stream.size_bytes is not None:
            headers["Content-Length"] = str(stream.size_bytes)
        return StreamingResponse(
            stream.iterator,
            media_type="application/octet-stream",
            headers=headers,
        )
    except BadRequest:
        raise HTTPException(status_code=404, detail="Telegram file not found")
    except (TelegramArtifactBridgeError, TimedOut, NetworkError) as error:
        logger.warning("Telegram file provider failed: %r", error)
        raise HTTPException(status_code=502, detail="Telegram file unavailable")


@app.post("/internal/progress")
async def internal_progress_handler(request: Request):
    if TELEGRAM_PROGRESS_CALLBACK_TOKEN and not hmac.compare_digest(
        request.headers.get("X-Progress-Token", ""),
        TELEGRAM_PROGRESS_CALLBACK_TOKEN,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid progress token",
        )
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError("Progress payload must be an object")
        event = payload.get("event") or {}
        target = payload.get("target") or {}
        if payload.get("client_type") != "telegram":
            return {"status": "ignored", "reason": "non-telegram client"}
        if not isinstance(event, dict) or not isinstance(target, dict):
            raise ValueError("Progress event and target must be objects")
        target_session = str(target.get("session_id") or "").strip()
        target_generation = target.get("session_generation")
        if (
            target_session
            and target_generation is not None
            and not session_generations.is_current(
                target_session,
                int(target_generation),
            )
        ):
            return {"status": "ignored", "reason": "stale session generation"}
        if event.get("visibility", "user") != "user":
            return {"status": "ignored", "reason": "non-user visibility"}
        if event.get("type") == "iteration_started":
            return {"status": "ignored", "reason": "debug event"}
        message = event.get("message")
        if not message:
            return {"status": "ignored", "reason": "empty message"}
        chat_id = target.get("chat_id") or target.get("conversation_id")
        message_id = target.get("message_id") or target.get("status_message_id")
        if chat_id is None or message_id is None:
            return {"status": "ignored", "reason": "missing target"}
        return {
            "status": "queued",
            "version": enqueue_progress_message(
                chat_id=int(chat_id),
                message_id=int(message_id),
                text=str(message),
            ),
        }
    except HTTPException:
        raise
    except Exception as error:
        logger.exception("Progress payload error: %r", error)
        raise HTTPException(status_code=400, detail="Invalid progress payload")


@app.get("/")
async def root():
    return {"service": "Telegram Bot Gateway", "status": "running"}


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "bot": application.bot.first_name if application.bot else "not initialized",
        "artifact_ingress": True,
        "file_provider_configured": bool(TELEGRAM_FILE_PROVIDER_TOKEN),
        "pending_media_groups": len(media_groups),
        "standalone_lock_count": await standalone_lock_pool.size(),
    }
