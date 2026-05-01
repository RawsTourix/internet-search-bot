import os
import re
import httpx
import asyncio
import uuid
import html
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, status
from telegram import Update, BotCommand
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.error import TimedOut, NetworkError, BadRequest
from datetime import datetime
from logging.handlers import RotatingFileHandler

# Импорт модулей
from .config import BOT_TOKEN, WEBHOOK_SECRET, WEBHOOK_DOMAIN, TELEGRAM_API_KEY, GATEWAY_URL
from ...utils.telegram_formatting import markdown_to_telegram_html, split_telegram_message

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

async def send_to_gateway(payload: dict) -> tuple[bool, str]:
    """Отправляет данные в Gateway и возвращает статус успеха и сообщение"""
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
            logger.info("Сообщение успешно отправлено в Gateway")
            return True, response.json().get("response", "Успешно отправлено в Gateway")

    except httpx.TimeoutException as e:
        logger.error(f"Таймаут при ожидании ответа от Gateway: {type(e).__name__}: {e!r}")
        return False, "Gateway обрабатывал запрос слишком долго и не успел вернуть ответ."

    except httpx.RequestError as e:
        logger.error(f"Ошибка при отправке в Gateway: {type(e).__name__}: {e!r}")
        return False, f"Не удалось подключиться к Gateway: {e}"

    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error from Gateway: {e.response.status_code} - {e.response.text}")
        return False, f"Ошибка от Gateway: {e.response.status_code} - {e.response.text}"

    except Exception as e:
        logger.exception(f"Неизвестная ошибка при отправке в Gateway: {type(e).__name__}: {e!r}")
        return False, f"Неизвестная ошибка: {e}"

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
    
    await update.message.reply_text(f"Ваш запрос принят в обработку...")

    success, message = await send_to_gateway(payload)
    if success:
        await send_telegram_markdown_reply(update, message)
        logger.info(
            f"Ответ на команду [id: {payload.get('id')}] "
            f"от {payload.get('user_name') or payload.get('user_id')}: {message}"
        )
    else:
        await send_telegram_markdown_reply(
            update,
            f"**Произошла ошибка при обработке запроса:**\n{message}"
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

    try:
        await update.message.reply_text(f"Сообщение принято! Обрабатываю...")
    except (TimedOut, NetworkError) as e:
        logger.warning(f"Не удалось отправить промежуточный ответ в Telegram: {e}")

    success, message = await send_to_gateway(payload)
    if success:
        await send_telegram_markdown_reply(update, message)
        logger.info(
            f"Ответ на сообщение [id: {payload.get('id')}] "
            f"от {payload.get('user_name') or payload.get('user_id')}: {message}"
        )
    else:
        await send_telegram_markdown_reply(
            update,
            f"**Произошла ошибка при обработке запроса:**\n{message}"
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
    html_text = markdown_to_telegram_html(text)
    chunks = split_telegram_message(html_text)

    for chunk in chunks:
        try:
            await telegram_reply_with_retries(
                update,
                chunk,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True
            )

        except BadRequest as e:
            # Если Telegram не принял HTML, отправляем обычный текст
            logger.warning(f"Ошибка Telegram HTML formatting: {e}")

            plain_chunk = html.unescape(re.sub(r"<[^>]+>", "", chunk))

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

@app.get("/")
async def root():
    return {"service": "Telegram Bot Gateway", "status": "running"}

@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "bot": application.bot.first_name if application.bot else "not initialized"
    }