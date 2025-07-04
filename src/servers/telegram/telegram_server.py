import os
import httpx
import asyncio
import uuid
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, status
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from datetime import datetime
from logging.handlers import RotatingFileHandler

# Импорт модулей
from .config import BOT_TOKEN, WEBHOOK_SECRET, WEBHOOK_DOMAIN, TELEGRAM_API_KEY, GATEWAY_URL

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
        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.post(
                f"{GATEWAY_URL}/message",
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": TELEGRAM_API_KEY # API ключ (внутренний)
                }
            )
            response.raise_for_status()
            logger.info(f"Сообщение успешно отправлено в Gateway")
            return True, response.json().get("response", "Успешно отправлено в Gateway")
    except httpx.RequestError as e:
        logger.error(f"Ошибка при отправке в Gateway: {e}")
        return False, f"Не удалось подключиться к Gateway: {e}"
    except httpx.HTTPStatusError as e:
        logger.error(f"HTTP error from Gateway: {e.response.status_code} - {e.response.text}")
        return False, f"Ошибка от Gateway: {e.response.status_code} - {e.response.text}"
    except Exception as e:
        logger.error(f"Неизвестная ошибка: {e}")
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
    
    await update.message.reply_text(f"Ваш запрос принят в обработку.")

    success, message = await send_to_gateway(payload)
    if success:
        await update.message.reply_text(message)
        logger.info(f"Ответ на команду [id: {payload.get('id')}] от {payload.get('user_name') or payload.get('user_id')}: {message}")
    else:
        await update.message.reply_text(f"Произошла ошибка при обработке запроса: {message}")
        logger.error(f"Ответ на команду [id: {payload.get('id')}] от {payload.get('user_name') or payload.get('user_id')}: {message}")

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

    await update.message.reply_text(f"Сообщение принято! Обрабатываю...")

    success, message = await send_to_gateway(payload)
    if success:
        await update.message.reply_text(message)
        logger.info(f"Ответ на сообщение [id: {payload.get('id')}] от {payload.get('user_name') or payload.get('user_id')}: {message}")
    else:
        await update.message.reply_text(f"Произошла ошибка при обработке запроса: {message}")
        logger.error(f"Ответ на сообщение [id: {payload.get('id')}] от {payload.get('user_name') or payload.get('user_id')}: {message}")

# Регистрация обработчиков
application.add_handler(CommandHandler(['start', 'status', 'help'], command_handler)) # Команды обрабатываются в API
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