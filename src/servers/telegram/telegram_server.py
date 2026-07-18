import os
import re
import httpx
import asyncio
import uuid
import logging
from typing import Any, Callable
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, status
from telegram import Update, BotCommand
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import TimedOut, NetworkError, BadRequest
from datetime import datetime
from logging.handlers import RotatingFileHandler

# Импорт модулей
from .config import (
    BOT_TOKEN,
    WEBHOOK_SECRET,
    WEBHOOK_DOMAIN,
    TELEGRAM_API_KEY,
    GATEWAY_URL,
    TELEGRAM_PROGRESS_CALLBACK_URL,
    TELEGRAM_PROGRESS_CALLBACK_TOKEN,
    PROGRESS_EDIT_MIN_INTERVAL,
    PROGRESS_MAX_TEXT_LENGTH,
    TELEGRAM_FINAL_EDIT_MAX_LENGTH,
    TELEGRAM_FINAL_DELIVERY_MODE,
)
from ...utils.telegram_formatting import markdown_to_telegram_html, split_telegram_message, split_markdown_for_telegram, markdown_to_plain_text

# Проверяем и создаем папку для логов
log_dir = "logging"
if not os.path.exists(log_dir):
    os.makedirs(log_dir, exist_ok=True)

# Настройка логирования
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

logger = logging.getLogger("TelegramServer")
logger.setLevel(logging.DEBUG)

file_handler = RotatingFileHandler(
    filename=os.path.join(log_dir, "telegram_server.log"),
    maxBytes=8*1024*1024,  # 8 MB
    encoding='utf-8'
)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)

logger.addHandler(file_handler)
logger.addHandler(console_handler)

# Инициализация Telegram Application
application = Application.builder().token(BOT_TOKEN).build()
progress_edit_state: dict[str, dict[str, Any]] = {}
progress_edit_queues: dict[
    str,
    asyncio.Queue[tuple[int, str]],
] = {}
progress_edit_workers: dict[str, asyncio.Task[None]] = {}
progress_edit_versions: dict[str, int] = {}

FINAL_ERROR_MESSAGES: dict[str, dict[str, str]] = {
    "ru": {
        "infrastructure_interruption": (
            "⚠️ Задача прервана из-за инфраструктурной ошибки.\n\n"
            "Тип: {error_type}\n"
            "Итерация: {iteration}\n"
            "Состояние задачи сохранено, её можно продолжить позже."
        ),
        "llm_configuration_error": (
            "⚠️ Задача остановлена из-за ошибки конфигурации LLM.\n\n"
            "Тип: {error_type}\n"
            "Итерация: {iteration}\n"
            "Проверь API URL, endpoint, model name или настройки провайдера."
        ),
        "agent_error": (
            "⚠️ Агент завершил задачу с ошибкой.\n\n"
            "Тип: {error_type}\n"
            "Итерация: {iteration}\n"
            "Подробности: {error_message}"
        ),
    },
    "en": {
        "infrastructure_interruption": (
            "⚠️ The task was interrupted by an infrastructure error.\n\n"
            "Type: {error_type}\n"
            "Iteration: {iteration}\n"
            "The task state has been saved and can be resumed later."
        ),
        "llm_configuration_error": (
            "⚠️ The task stopped because of an LLM configuration error.\n\n"
            "Type: {error_type}\n"
            "Iteration: {iteration}\n"
            "Check the API URL, endpoint, model name, or provider settings."
        ),
        "agent_error": (
            "⚠️ The agent finished with an error.\n\n"
            "Type: {error_type}\n"
            "Iteration: {iteration}\n"
            "Details: {error_message}"
        ),
    },
}

async def send_to_gateway(payload: dict) -> tuple[bool, str, dict[str, Any]]:
    """Отправляет данные в Gateway и возвращает успех, ответ и metadata."""
    try:
        timeout = httpx.Timeout(
            connect=10.0,
            read=1800.0,
            write=30.0,
            pool=10.0
        )

        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{GATEWAY_URL}/message",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": TELEGRAM_API_KEY
                }
            )
            response.raise_for_status()
            data = response.json()
            logger.info("Сообщение успешно отправлено в Gateway")
            return (
                True,
                data.get("response", "Успешно отправлено в Gateway"),
                data.get("metadata", {}) or {},
            )

    except httpx.TimeoutException as e:
        logger.error(f"Таймаут при ожидании ответа от Gateway: {type(e).__name__}: {e!r}")
        return False, "Gateway обрабатывал запрос слишком долго и не успел вернуть ответ.", {}

    except httpx.RequestError as e:
        logger.error(f"Ошибка при отправке в Gateway: {type(e).__name__}: {e!r}")
        return False, f"Не удалось подключиться к Gateway: {e}", {}

    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error from Gateway: {e.response.status_code} - {e.response.text}")
        return False, f"Ошибка от Gateway: {e.response.status_code} - {e.response.text}", {}

    except Exception as e:
        logger.exception(f"Неизвестная ошибка при отправке в Gateway: {type(e).__name__}: {e!r}")
        return False, f"Неизвестная ошибка: {e}", {}


def detect_progress_locale(update: Update) -> str:
    language_code = getattr(update.effective_user, "language_code", None)
    if language_code and language_code.lower().startswith("en"):
        return "en"
    return "ru"


def normalize_locale(value: str | None) -> str:
    value = (value or "ru").lower().strip()
    return "en" if value.startswith("en") else "ru"


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

    http_match = re.search(r"\bHTTP\s*(\d{3})\b", text, flags=re.IGNORECASE)
    if not http_match:
        http_match = re.search(
            r"\bLLM\s+API\s*:\s*(\d{3})\b",
            text,
            flags=re.IGNORECASE,
        )
    if http_match:
        return f"LLMHTTPError / HTTP {http_match.group(1)}"

    status_match = re.search(
        r"status[_ ]code[=:\s]+(\d{3})",
        text,
        flags=re.IGNORECASE,
    )
    if status_match:
        return f"LLMHTTPError / HTTP {status_match.group(1)}"
    if "LLMTransportError" in text:
        return "LLMTransportError"
    if "LLMHTTPError" in text:
        return "LLMHTTPError"
    return "RuntimeError"


def extract_llm_http_status(error_type: str) -> int | None:
    match = re.search(r"\bHTTP\s+(\d{3})\b", error_type, flags=re.IGNORECASE)
    if not match:
        return None

    return int(match.group(1))


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
    llm_http_status = extract_llm_http_status(error_type)

    if error_kind == "llm_configuration_error":
        key = "llm_configuration_error"
    elif (
        error_kind != "infrastructure_interruption"
        and llm_http_status in {400, 401, 403, 404, 422}
    ):
        key = "llm_configuration_error"
    else:
        is_infra = (
            error_kind == "infrastructure_interruption"
            or "LLMTransportError" in error_type
            or "LLMTimeoutError" in error_type
            or "ConnectError" in error_type
            or llm_http_status in {429, 500, 502, 503, 504}
        )
        key = "infrastructure_interruption" if is_infra else "agent_error"

    return FINAL_ERROR_MESSAGES[locale_name][key].format(
        error_type=error_type,
        iteration=iterations,
        error_message=error_message,
    )


async def send_initial_status_message(update: Update, text: str):
    try:
        return await update.message.reply_text(text)
    except (TimedOut, NetworkError) as e:
        logger.warning(f"Не удалось отправить промежуточный ответ в Telegram: {e!r}")
        return None


def attach_progress_metadata(*, payload: dict, update: Update, status_message) -> None:
    metadata = payload.setdefault("metadata", {})
    metadata["progress_locale"] = detect_progress_locale(update)

    if not status_message:
        return

    chat_id = update.effective_chat.id
    status_message_id = status_message.message_id
    metadata["status_message_id"] = status_message_id
    metadata["progress_request_id"] = payload["id"]
    metadata["progress_target"] = {
        "chat_id": chat_id,
        "message_id": status_message_id,
    }
    if TELEGRAM_PROGRESS_CALLBACK_URL:
        metadata["progress_callback_url"] = TELEGRAM_PROGRESS_CALLBACK_URL
    if TELEGRAM_PROGRESS_CALLBACK_TOKEN:
        metadata["progress_callback_token"] = TELEGRAM_PROGRESS_CALLBACK_TOKEN

async def command_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команд"""
    full_text = update.message.text
    command = full_text.split()[0] # Команда
    args = full_text.split()[1:] if len(full_text.split()) > 1 else [] # Аргументы
        
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    payload = {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.now().isoformat(),
        "client_type": "telegram",
        "message_type": "command",
        "content": full_text,
        "user_id": str(user.id),
        "user_name": user.full_name,
        "metadata": {
            "chat_id": chat_id,
            "message_id": update.message.message_id
        },
        "command": command,
        "arguments": args
    }

    logger.debug(f"Получена команда: {payload}")
    logger.info(f"Команда [id: {payload.get('id')}] от {payload.get('user_name') or payload.get('user_id')}: {payload.get('command')}")
    
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
    locale_name = payload.get("metadata", {}).get("progress_locale", "ru")
    agent_failed = is_agent_error(metadata)

    if success and not agent_failed:
        await finish_status_or_send_reply(
            update=update,
            status_message=status_message,
            text=message,
            delivery_mode=TELEGRAM_FINAL_DELIVERY_MODE,
        )
        logger.info(
            f"Ответ на команду [id: {payload.get('id')}] "
            f"от {payload.get('user_name') or payload.get('user_id')}: {message}"
        )
    elif success and agent_failed:
        formatted_error = format_agent_error_for_telegram(
            message,
            metadata,
            locale_name=locale_name,
        )
        await finish_status_or_send_reply(
            update=update,
            status_message=status_message,
            text=formatted_error,
            delivery_mode="send_new",
        )
        logger.error(
            f"Ошибка агента для команды [id: {payload.get('id')}]: {message}"
        )
    else:
        await finish_status_or_send_reply(
            update=update,
            status_message=status_message,
            text=f"**Произошла ошибка при обработке запроса:**\n{message}",
            delivery_mode="send_new",
        )
        logger.error(
            f"Ответ на команду [id: {payload.get('id')}] "
            f"от {payload.get('user_name') or payload.get('user_id')}: {message}"
        )

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений"""
    user = update.effective_user
    chat_id = update.effective_chat.id
    
    payload = {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.now().isoformat(),
        "client_type": "telegram",
        "message_type": "text",
        "content": update.message.text,
        "user_id": str(user.id),
        "user_name": user.full_name,
        "metadata": {
            "chat_id": chat_id,
            "message_id": update.message.message_id
        }
    }
    
    logger.debug(f"Получено сообщение: {payload}")
    logger.info(f"Сообщение [id: {payload.get('id')}] от {payload.get('user_name') or payload.get('user_id')}: {payload.get('content')}")

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
    locale_name = payload.get("metadata", {}).get("progress_locale", "ru")
    agent_failed = is_agent_error(metadata)

    if success and not agent_failed:
        await finish_status_or_send_reply(
            update=update,
            status_message=status_message,
            text=message,
            delivery_mode=TELEGRAM_FINAL_DELIVERY_MODE,
        )
        logger.info(
            f"Ответ на сообщение [id: {payload.get('id')}] "
            f"от {payload.get('user_name') or payload.get('user_id')}: {message}"
        )
    elif success and agent_failed:
        formatted_error = format_agent_error_for_telegram(
            message,
            metadata,
            locale_name=locale_name,
        )
        await finish_status_or_send_reply(
            update=update,
            status_message=status_message,
            text=formatted_error,
            delivery_mode="send_new",
        )
        logger.error(
            f"Ошибка агента для сообщения [id: {payload.get('id')}]: {message}"
        )
    else:
        await finish_status_or_send_reply(
            update=update,
            status_message=status_message,
            text=f"**Произошла ошибка при обработке запроса:**\n{message}",
            delivery_mode="send_new",
        )
        logger.error(
            f"Ответ на сообщение [id: {payload.get('id')}] "
            f"от {payload.get('user_name') or payload.get('user_id')}: {message}"
        )

async def telegram_reply_with_retries(
    update: Update,
    text: str,
    *,
    parse_mode=None,
    disable_web_page_preview: bool = True,
    max_retries: int = 5,
    base_delay: float = 2.0
):
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            return await update.message.reply_text(
                text,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview
            )
        
        except BadRequest:
            # Ошибка HTML/Markdown-разметки. Повторять бессмысленно.
            raise

        except (TimedOut, NetworkError) as e:
            last_error = e
            delay = base_delay * (2 ** (attempt - 1))

            logger.warning(
                f"Telegram send timeout/network error. "
                f"Попытка {attempt}/{max_retries}, повтор через {delay:.1f} сек: {e!r}"
            )

            if attempt >= max_retries:
                break

            await asyncio.sleep(delay)

    raise last_error

async def send_telegram_markdown_reply(update, text: str):
    markdown_chunks = split_markdown_for_telegram(text)

    for markdown_chunk in markdown_chunks:
        html_chunk = markdown_to_telegram_html(markdown_chunk)

        try:
            await telegram_reply_with_retries(
                update,
                html_chunk,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )

        except BadRequest as e:
            # Если Telegram не принял HTML, отправляем обычный текст
            logger.warning(f"Ошибка Telegram HTML formatting: {e}")

            plain_chunk = markdown_to_plain_text(markdown_chunk)

            await telegram_reply_with_retries(
                update,
                plain_chunk,
                parse_mode=None,
                disable_web_page_preview=True
            )

        except (TimedOut, NetworkError) as e:
            logger.error(
                f"Не удалось отправить сообщение в Telegram после retry: {e!r}"
            )
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
    last_error = None

    for attempt in range(1, max_retries + 1):
        if is_stale is not None and is_stale():
            logger.debug(
                "Пропущен устаревший progress edit: chat_id=%s "
                "message_id=%s attempt=%s",
                chat_id,
                message_id,
                attempt,
            )
            return None
        try:
            return await application.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
            )
        except BadRequest as e:
            if "message is not modified" in str(e).lower():
                return None
            raise
        except (TimedOut, NetworkError) as e:
            last_error = e
            if is_stale is not None and is_stale():
                logger.debug(
                    "Отменён retry устаревшего progress edit: "
                    "chat_id=%s message_id=%s attempt=%s",
                    chat_id,
                    message_id,
                    attempt,
                )
                return None
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning(
                "Telegram edit timeout/network error. Попытка %s/%s, "
                "повтор через %.1f сек: %r",
                attempt,
                max_retries,
                delay,
                e,
            )
            if attempt >= max_retries:
                break
            await asyncio.sleep(delay)

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
    edit_state = progress_edit_state.get(key) or {}

    if edit_state.get("last_text") == text:
        return
    since_last_edit = now - float(edit_state.get("last_edit_at", 0.0))
    if since_last_edit < PROGRESS_EDIT_MIN_INTERVAL:
        await asyncio.sleep(PROGRESS_EDIT_MIN_INTERVAL - since_last_edit)
        if is_stale is not None and is_stale():
            return
        now = asyncio.get_running_loop().time()

    try:
        await edit_telegram_message_with_retries(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=None,
            disable_web_page_preview=True,
            is_stale=is_stale,
        )
        if is_stale is not None and is_stale():
            return
        progress_edit_state[key] = {
            "last_text": text,
            "last_edit_at": now,
        }
        logger.debug(
            "Progress message edited: chat_id=%s message_id=%s text=%r",
            chat_id,
            message_id,
            text,
        )
    except Exception as e:
        logger.warning(f"Не удалось обновить progress-сообщение: {e!r}")


async def _run_progress_edit_worker(
    *,
    key: str,
    chat_id: int,
    message_id: int,
    queue: asyncio.Queue[tuple[int, str]],
) -> None:
    """Serialize edits per Telegram message and coalesce queued stages."""
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
        logger.exception(
            "Ошибка фоновой обработки progress edit: "
            "chat_id=%s message_id=%s",
            chat_id,
            message_id,
        )
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
    """Queue a progress edit without blocking the Gateway callback."""
    key = f"{chat_id}:{message_id}"
    version = progress_edit_versions.get(key, 0) + 1
    progress_edit_versions[key] = version

    queue = progress_edit_queues.get(key)
    if queue is None:
        queue = asyncio.Queue()
        progress_edit_queues[key] = queue
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
    """Invalidate and stop pending progress edits before final delivery."""
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
    """Stop all background edits during shutdown and isolated tests."""
    keys = set(progress_edit_workers) | set(progress_edit_queues)
    for key in keys:
        chat_id, message_id = key.split(":", maxsplit=1)
        await stop_progress_edits(
            chat_id=int(chat_id),
            message_id=int(message_id),
        )


async def finish_status_or_send_reply(
    *,
    update: Update,
    status_message,
    text: str,
    force_reply_if_long: bool = False,
    delivery_mode: str | None = None,
) -> None:
    delivery_mode = (delivery_mode or TELEGRAM_FINAL_DELIVERY_MODE).lower().strip()
    if delivery_mode not in {"send_new", "edit_status", "auto"}:
        logger.warning(
            "Неизвестный TELEGRAM_FINAL_DELIVERY_MODE=%r; используется send_new",
            delivery_mode,
        )
        delivery_mode = "send_new"

    if not status_message:
        await send_telegram_markdown_reply(update, text)
        return

    await stop_progress_edits(
        chat_id=update.effective_chat.id,
        message_id=status_message.message_id,
    )

    raw_text = text or ""

    if delivery_mode == "send_new":
        await send_telegram_markdown_reply(update, raw_text)
        return

    markdown_chunks = split_markdown_for_telegram(raw_text)
    should_send_separately = (
        force_reply_if_long
        or len(raw_text) > TELEGRAM_FINAL_EDIT_MAX_LENGTH
        or len(markdown_chunks) != 1
    )

    if should_send_separately:
        await send_telegram_markdown_reply(update, raw_text)
        return

    markdown_chunk = markdown_chunks[0]
    html_chunk = markdown_to_telegram_html(markdown_chunk)
    try:
        await edit_telegram_message_with_retries(
            chat_id=update.effective_chat.id,
            message_id=status_message.message_id,
            text=html_chunk,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except BadRequest as e:
        logger.warning(f"Ошибка Telegram HTML formatting при edit: {e}")
        plain_chunk = markdown_to_plain_text(markdown_chunk)
        try:
            await edit_telegram_message_with_retries(
                chat_id=update.effective_chat.id,
                message_id=status_message.message_id,
                text=plain_chunk,
                parse_mode=None,
                disable_web_page_preview=True,
            )
        except (TimedOut, NetworkError) as retry_error:
            logger.error(
                f"Не удалось отредактировать финальное сообщение: {retry_error!r}"
            )
            await send_telegram_markdown_reply(update, raw_text)
    except (TimedOut, NetworkError) as e:
        logger.error(
            f"Не удалось отредактировать финальное сообщение после retry: {e!r}"
        )
        await send_telegram_markdown_reply(update, raw_text)

# Регистрация обработчиков
application.add_handler(CommandHandler(['start', 'status', 'reset', 'help'], command_handler)) # Команды обрабатываются в API
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Управление жизненным циклом приложения"""
    # Инициализация бота
    await application.initialize()
    await application.start()
    
    # Установка вебхука
    await application.bot.set_webhook(
        url=f"{WEBHOOK_DOMAIN}/telegram/webhook",
        secret_token=WEBHOOK_SECRET
    )
    logger.info(f"Вебхук установлен: {WEBHOOK_DOMAIN}/telegram/webhook")

    commands = [
        BotCommand("start", "Приветствие"),
        BotCommand("status", "Статус системы"),
        BotCommand("reset", "Очистка памяти"),
        BotCommand("help", "Справка"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info(f"Список команд задан: {[command.command for command in commands]}")
    
    yield

    await stop_all_progress_edits()
    
    # Очистка при завершении
    await application.bot.delete_webhook()
    await application.stop()
    await application.shutdown()
    logger.info("Вебхук удален, бот остановлен")

app = FastAPI(lifespan=lifespan, title="Telegram Bot Gateway", version="1.0.0")

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    """Webhook endpoint для Telegram Bot API"""
    # Проверка секретного токена
    if request.headers.get("X-Telegram-Bot-Api-Secret-Token") != WEBHOOK_SECRET:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    
    try:
        update_data = await request.json()
        update = Update.de_json(update_data, application.bot)
        
        # Асинхронная обработка без ожидания
        asyncio.create_task(application.process_update(update))
        
        return {"status": "ok"}
    except Exception as e:
        logger.exception(f"Ошибка обработки webhook: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/internal/progress")
async def internal_progress_handler(request: Request):
    """Принимает live progress events от Gateway."""
    if TELEGRAM_PROGRESS_CALLBACK_TOKEN:
        token = request.headers.get("X-Progress-Token")
        if token != TELEGRAM_PROGRESS_CALLBACK_TOKEN:
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
        if not isinstance(event, dict) or not isinstance(target, dict):
            raise ValueError("Progress event and target must be objects")

        if payload.get("client_type") != "telegram":
            return {"status": "ignored", "reason": "non-telegram client"}

        if event.get("visibility", "user") != "user":
            return {"status": "ignored", "reason": "non-user visibility"}
        if event.get("type") == "iteration_started":
            return {"status": "ignored", "reason": "debug event"}

        message = event.get("message")
        if not message:
            return {"status": "ignored", "reason": "empty message"}

        chat_id = target.get("chat_id")
        message_id = target.get("message_id") or target.get("status_message_id")
        if chat_id is None or message_id is None:
            logger.warning(f"Progress event без chat_id/message_id: {payload}")
            return {"status": "ignored", "reason": "missing target"}

        version = enqueue_progress_message(
            chat_id=int(chat_id),
            message_id=int(message_id),
            text=str(message),
        )
        return {"status": "queued", "version": version}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Ошибка обработки progress event: {e!r}")
        raise HTTPException(status_code=400, detail="Invalid progress payload")

@app.get("/")
async def root():
    return {"service": "Telegram Bot Gateway", "status": "running"}

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "bot": application.bot.first_name if application.bot else "not initialized"
    }
