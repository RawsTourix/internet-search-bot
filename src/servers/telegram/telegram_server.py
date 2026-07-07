import os
import re
import httpx
import asyncio
import uuid
import html
import logging
from typing import Any
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
    if success:
        await finish_status_or_send_reply(
            update=update,
            status_message=status_message,
            text=message,
        )
        logger.info(
            f"Ответ на команду [id: {payload.get('id')}] "
            f"от {payload.get('user_name') or payload.get('user_id')}: {message}"
        )
    else:
        await finish_status_or_send_reply(
            update=update,
            status_message=status_message,
            text=f"**Произошла ошибка при обработке запроса:**\n{message}",
            force_reply_if_long=True,
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
    if success:
        await finish_status_or_send_reply(
            update=update,
            status_message=status_message,
            text=message,
        )
        logger.info(
            f"Ответ на сообщение [id: {payload.get('id')}] "
            f"от {payload.get('user_name') or payload.get('user_id')}: {message}"
        )
    else:
        await finish_status_or_send_reply(
            update=update,
            status_message=status_message,
            text=f"**Произошла ошибка при обработке запроса:**\n{message}",
            force_reply_if_long=True,
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
):
    last_error = None

    for attempt in range(1, max_retries + 1):
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
) -> None:
    text = (text or "").strip()
    if not text:
        return
    if len(text) > PROGRESS_MAX_TEXT_LENGTH:
        text = text[:PROGRESS_MAX_TEXT_LENGTH] + "…"

    key = f"{chat_id}:{message_id}"
    now = asyncio.get_running_loop().time()
    edit_state = progress_edit_state.get(key) or {}

    if edit_state.get("last_text") == text:
        return
    if now - float(edit_state.get("last_edit_at", 0.0)) < PROGRESS_EDIT_MIN_INTERVAL:
        return

    try:
        await edit_telegram_message_with_retries(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode=None,
            disable_web_page_preview=True,
        )
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


async def finish_status_or_send_reply(
    *,
    update: Update,
    status_message,
    text: str,
    force_reply_if_long: bool = False,
) -> None:
    if not status_message:
        await send_telegram_markdown_reply(update, text)
        return

    progress_edit_state.pop(
        f"{update.effective_chat.id}:{status_message.message_id}",
        None,
    )

    raw_text = text or ""
    markdown_chunks = split_markdown_for_telegram(raw_text)
    should_send_separately = (
        force_reply_if_long
        or len(raw_text) > TELEGRAM_FINAL_EDIT_MAX_LENGTH
        or len(markdown_chunks) != 1
    )

    if should_send_separately:
        try:
            await edit_telegram_message_with_retries(
                chat_id=update.effective_chat.id,
                message_id=status_message.message_id,
                text="✅ Готово. Отправляю результат ниже.",
                parse_mode=None,
                disable_web_page_preview=True,
            )
        except Exception as e:
            logger.warning(
                f"Не удалось обновить status-сообщение перед ответом: {e!r}"
            )
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

        await maybe_edit_progress_message(
            chat_id=int(chat_id),
            message_id=int(message_id),
            text=str(message),
        )
        return {"status": "ok"}
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
