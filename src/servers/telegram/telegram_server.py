import asyncio
import hmac
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Any, Callable

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

from .artifact_bridge import (
    DebouncedBatchRunner,
    TelegramArtifactBridgeError,
    TelegramArtifactGatewayClient,
    build_telegram_input_envelope,
    extract_telegram_attachments,
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
    TELEGRAM_MEDIA_GROUP_COMMIT_DELAY_SECONDS,
    TELEGRAM_PROGRESS_CALLBACK_TOKEN,
    TELEGRAM_PROGRESS_CALLBACK_URL,
    WEBHOOK_DOMAIN,
    WEBHOOK_SECRET,
)
from .runtime_state import KeyedAsyncLockPool
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
artifact_gateway = TelegramArtifactGatewayClient(
    gateway_url=GATEWAY_URL,
    api_key=TELEGRAM_API_KEY,
    delivery_spool_memory_bytes=TELEGRAM_DELIVERY_SPOOL_MEMORY_BYTES,
)
media_group_runner = DebouncedBatchRunner()
standalone_lock_pool = KeyedAsyncLockPool()

progress_edit_state: dict[str, dict[str, Any]] = {}
progress_edit_queues: dict[str, asyncio.Queue[tuple[int, str]]] = {}
progress_edit_workers: dict[str, asyncio.Task[None]] = {}
progress_edit_versions: dict[str, int] = {}
media_groups: dict[str, "PendingMediaGroup"] = {}
media_groups_guard = asyncio.Lock()


@dataclass(slots=True)
class PendingMediaGroup:
    key: str
    input_batch_id: str | None
    session_id: str
    progress_locale: str
    update: Update
    status_message: Any
    response_metadata: dict[str, Any]
    failed: bool = False


FINAL_ERROR_MESSAGES: dict[str, dict[str, str]] = {
    "ru": {
        "infrastructure_interruption": (
            "⚠️ Задача прервана из-за инфраструктурной ошибки.\n\n"
            "Тип: {error_type}\nИтерация: {iteration}\n"
            "Состояние задачи сохранено, её можно продолжить позже."
        ),
        "context_limit_interruption": (
            "⚠️ Задача приостановлена: рабочий контекст достиг предельного "
            "размера.\n\nТип: {error_type}\nИтерация: {iteration}\n"
            "Состояние задачи сохранено для продолжения."
        ),
        "llm_configuration_error": (
            "⚠️ Задача остановлена из-за ошибки конфигурации LLM.\n\n"
            "Тип: {error_type}\nИтерация: {iteration}\n"
            "Проверь API URL, endpoint, model name или настройки провайдера."
        ),
        "agent_error": (
            "⚠️ Агент завершил задачу с ошибкой.\n\n"
            "Тип: {error_type}\nИтерация: {iteration}\n"
            "Подробности: {error_message}"
        ),
    },
    "en": {
        "infrastructure_interruption": (
            "⚠️ The task was interrupted by an infrastructure error.\n\n"
            "Type: {error_type}\nIteration: {iteration}\n"
            "The task state has been saved and can be resumed later."
        ),
        "context_limit_interruption": (
            "⚠️ The task was paused because the working context reached its "
            "limit.\n\nType: {error_type}\nIteration: {iteration}\n"
            "The task state has been saved for continuation."
        ),
        "llm_configuration_error": (
            "⚠️ The task stopped because of an LLM configuration error.\n\n"
            "Type: {error_type}\nIteration: {iteration}\n"
            "Check the API URL, endpoint, model name, or provider settings."
        ),
        "agent_error": (
            "⚠️ The agent finished with an error.\n\n"
            "Type: {error_type}\nIteration: {iteration}\n"
            "Details: {error_message}"
        ),
    },
}


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
        key = "context_limit_interruption"
    elif error_kind == "llm_configuration_error" or (
        error_kind != "infrastructure_interruption"
        and http_status in {400, 401, 403, 404, 422}
    ):
        key = "llm_configuration_error"
    elif (
        error_kind == "infrastructure_interruption"
        or "LLMTransportError" in error_type
        or "LLMTimeoutError" in error_type
        or http_status in {429, 500, 502, 503, 504}
    ):
        key = "infrastructure_interruption"
    else:
        key = "agent_error"
    return FINAL_ERROR_MESSAGES[locale_name][key].format(
        error_type=error_type,
        iteration=iterations,
        error_message=error_message,
    )


def _safe_transport_error(error: BaseException) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        return f"Gateway вернул HTTP {error.response.status_code}."
    if isinstance(error, (httpx.TimeoutException, TimedOut)):
        return "Истекло время ожидания обработки файла."
    if isinstance(error, (httpx.RequestError, NetworkError)):
        return "Не удалось связаться с сервисом обработки файлов."
    if isinstance(error, TelegramArtifactBridgeError):
        return "Не удалось безопасно обработать файл. Повторите отправку позже."
    return "Во время обработки файла произошла внутренняя ошибка."


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
                data.get("response", "Успешно отправлено в Gateway"),
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


def _progress_metadata(update: Update, status_message: Any) -> dict[str, Any]:
    message = update.effective_message
    metadata: dict[str, Any] = {
        "chat_id": update.effective_chat.id,
        "message_id": message.message_id,
        "progress_locale": detect_progress_locale(update),
    }
    if status_message is not None:
        metadata.update({
            "status_message_id": status_message.message_id,
            "progress_request_id": str(update.update_id),
            "progress_target": {
                "chat_id": update.effective_chat.id,
                "message_id": status_message.message_id,
            },
        })
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
        _progress_metadata(update, status_message)
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
            if attempt >= max_retries or (
                is_stale is not None and is_stale()
            ):
                break
            await asyncio.sleep(base_delay * (2 ** (attempt - 1)))
    if last_error is not None:
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
) -> None:
    mode = (delivery_mode or TELEGRAM_FINAL_DELIVERY_MODE).lower().strip()
    if mode not in {"send_new", "edit_status", "auto"}:
        mode = "send_new"
    if status_message is None:
        await send_telegram_markdown_reply(update, text)
        return
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
        await send_telegram_markdown_reply(update, raw)
        return
    chunk = chunks[0]
    try:
        await edit_telegram_message_with_retries(
            chat_id=update.effective_chat.id,
            message_id=status_message.message_id,
            text=markdown_to_telegram_html(chunk),
            parse_mode=ParseMode.HTML,
        )
    except BadRequest:
        try:
            await edit_telegram_message_with_retries(
                chat_id=update.effective_chat.id,
                message_id=status_message.message_id,
                text=markdown_to_plain_text(chunk),
                parse_mode=None,
            )
        except (TimedOut, NetworkError):
            await send_telegram_markdown_reply(update, raw)
    except (TimedOut, NetworkError):
        await send_telegram_markdown_reply(update, raw)


async def _deliver_agent_result(
    *,
    update: Update,
    status_message: Any,
    success: bool,
    message: str,
    metadata: dict[str, Any],
    session_id: str,
) -> None:
    locale_name = normalize_locale(
        metadata.get("progress_locale") or detect_progress_locale(update)
    )
    artifacts = metadata.get("artifacts") or []
    if success and not is_agent_error(metadata):
        final_text = (message or "").strip() or (
            "Файл готов." if artifacts else "Готово."
        )
        await finish_status_or_send_reply(
            update=update,
            status_message=status_message,
            text=final_text,
            delivery_mode=TELEGRAM_FINAL_DELIVERY_MODE,
        )
        outcomes = await artifact_gateway.deliver_selected(
            bot=application.bot,
            artifacts=list(artifacts),
            session_id=session_id,
            chat_id=update.effective_chat.id,
            message_thread_id=getattr(
                update.effective_message,
                "message_thread_id",
                None,
            ),
            reply_to_message_id=update.effective_message.message_id,
        )
        if any(item.state in {"failed", "unknown"} for item in outcomes):
            await send_telegram_markdown_reply(
                update,
                "⚠️ Не все подготовленные файлы удалось подтвердить как "
                "доставленные. Они сохранены в журнале доставки.",
            )
        return
    text = (
        format_agent_error_for_telegram(
            message,
            metadata,
            locale_name=locale_name,
        )
        if success
        else f"**Произошла ошибка при обработке запроса:**\n{message}"
    )
    await finish_status_or_send_reply(
        update=update,
        status_message=status_message,
        text=text,
        delivery_mode="send_new",
    )


def _session_for_update(update: Update) -> str:
    return telegram_session_id(
        update.effective_chat.id,
        getattr(update.effective_message, "message_thread_id", None),
    )


async def command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    full_text = update.effective_message.text or ""
    words = full_text.split()
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
    status_message = await send_initial_status_message(
        update,
        "Команда принята. Обрабатываю…",
    )
    attach_progress_metadata(
        payload=payload,
        update=update,
        status_message=status_message,
    )
    success, message, metadata = await send_to_gateway(payload)
    await _deliver_agent_result(
        update=update,
        status_message=status_message,
        success=success,
        message=message,
        metadata=metadata,
        session_id=_session_for_update(update),
    )


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payload = {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.now().isoformat(),
        "client_type": "telegram",
        "message_type": "text",
        "content": update.effective_message.text or "",
        "user_id": str(update.effective_user.id),
        "user_name": update.effective_user.full_name,
        "metadata": {
            "chat_id": update.effective_chat.id,
            "message_id": update.effective_message.message_id,
            "session_id": _session_for_update(update),
        },
    }
    status_message = await send_initial_status_message(
        update,
        "Сообщение принято. Обрабатываю…",
    )
    attach_progress_metadata(
        payload=payload,
        update=update,
        status_message=status_message,
    )
    success, message, metadata = await send_to_gateway(payload)
    await _deliver_agent_result(
        update=update,
        status_message=status_message,
        success=success,
        message=message,
        metadata=metadata,
        session_id=_session_for_update(update),
    )


async def _finish_group(group: PendingMediaGroup) -> None:
    try:
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
                text="Этот альбом уже был обработан; повторный запуск пропущен.",
                delivery_mode="send_new",
            )
            return
        metadata = payload.get("metadata", {}) or {}
        metadata.setdefault("progress_locale", group.progress_locale)
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
            message=_safe_transport_error(error),
            metadata={"progress_locale": group.progress_locale},
            session_id=group.session_id,
        )
    finally:
        async with media_groups_guard:
            if media_groups.get(group.key) is group:
                media_groups.pop(group.key, None)


async def _process_standalone_attachment(update: Update) -> None:
    status_message = await send_initial_status_message(
        update,
        "Файл принят. Загружаю и обрабатываю…",
    )
    progress_locale = detect_progress_locale(update)
    session_id = _session_for_update(update)
    try:
        envelope = build_telegram_input_envelope(
            update,
            bot_instance_id=TELEGRAM_BOT_INSTANCE_ID,
            response_metadata=_progress_metadata(update, status_message),
        )
        submission = await artifact_gateway.submit_envelope(
            envelope,
            progress_locale=progress_locale,
        )
        if submission.get("status") == "failed":
            raise TelegramArtifactBridgeError("Ingress rejected the attachment")
        if submission.get("duplicate") and submission.get("status") == "committed":
            await finish_status_or_send_reply(
                update=update,
                status_message=status_message,
                text="Этот файл уже был принят ранее; повторный запуск пропущен.",
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
                text="Этот файл уже был обработан; повторный запуск пропущен.",
                delivery_mode="send_new",
            )
            return
        metadata = payload.get("metadata", {}) or {}
        metadata.setdefault("progress_locale", progress_locale)
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
        await _deliver_agent_result(
            update=update,
            status_message=status_message,
            success=False,
            message=_safe_transport_error(error),
            metadata={"progress_locale": progress_locale},
            session_id=session_id,
        )


async def attachment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not extract_telegram_attachments(message):
        await message.reply_text("Этот тип вложения пока не поддерживается.")
        return
    media_group_id = getattr(message, "media_group_id", None)
    if media_group_id is None:
        key = (
            f"{TELEGRAM_BOT_INSTANCE_ID}:{update.update_id}:"
            f"{message.message_id}"
        )
        async with standalone_lock_pool.hold(key):
            await _process_standalone_attachment(update)
        return

    thread_id = getattr(message, "message_thread_id", None)
    group_key = (
        f"{TELEGRAM_BOT_INSTANCE_ID}:{update.effective_chat.id}:"
        f"{thread_id or '-'}:{media_group_id}"
    )
    session_id = _session_for_update(update)
    progress_locale = detect_progress_locale(update)
    created_group = False
    async with media_groups_guard:
        group = media_groups.get(group_key)
        if group is None:
            status_message = await send_initial_status_message(
                update,
                "Альбом принят. Жду остальные файлы…",
            )
            group = PendingMediaGroup(
                key=group_key,
                input_batch_id=None,
                session_id=session_id,
                progress_locale=progress_locale,
                update=update,
                status_message=status_message,
                response_metadata=_progress_metadata(update, status_message),
            )
            media_groups[group_key] = group
            created_group = True

    try:
        envelope = build_telegram_input_envelope(
            update,
            bot_instance_id=TELEGRAM_BOT_INSTANCE_ID,
            response_metadata=group.response_metadata,
        )
        submission = await artifact_gateway.submit_envelope(
            envelope,
            progress_locale=progress_locale,
        )
        if submission.get("status") == "failed":
            group.failed = True
            raise TelegramArtifactBridgeError("Ingress rejected a media-group item")
        if submission.get("status") == "committed" and submission.get("duplicate"):
            if created_group:
                async with media_groups_guard:
                    if media_groups.get(group_key) is group:
                        media_groups.pop(group_key, None)
                await finish_status_or_send_reply(
                    update=update,
                    status_message=group.status_message,
                    text="Этот альбом уже был обработан; повторный запуск пропущен.",
                    delivery_mode="send_new",
                )
            return
        batch_id = str(submission.get("input_batch_id") or "")
        if not batch_id:
            raise TelegramArtifactBridgeError("Gateway returned no input batch ID")
        group.input_batch_id = batch_id
        scheduled = await media_group_runner.schedule(
            group_key,
            delay_seconds=TELEGRAM_MEDIA_GROUP_COMMIT_DELAY_SECONDS,
            callback=lambda: _finish_group(group),
            reset=not bool(submission.get("duplicate")),
        )
        if not scheduled and not submission.get("duplicate"):
            logger.warning(
                "Late media-group item arrived while commit was already running: %s",
                group_key,
            )
    except Exception as error:
        group.failed = True
        logger.exception("Telegram media-group member failed: %r", error)
        async with media_groups_guard:
            if media_groups.get(group_key) is group:
                media_groups.pop(group_key, None)
        await _deliver_agent_result(
            update=group.update,
            status_message=group.status_message,
            success=False,
            message=_safe_transport_error(error),
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
    await media_group_runner.cancel_all()
    await stop_all_progress_edits()
    await application.bot.delete_webhook()
    await application.stop()
    await application.shutdown()
    logger.info("Telegram bot stopped")


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
