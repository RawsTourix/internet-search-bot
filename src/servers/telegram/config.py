import logging
import os
from dotenv import load_dotenv

# Загрузка переменных из .env
load_dotenv()

# Токен телеграмм бота
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Настройки вебхука
WEBHOOK_DOMAIN = os.getenv("WEBHOOK_DOMAIN")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

# Настройка API
TELEGRAM_API_KEY = os.getenv("TELEGRAM_API_KEY")

# Получение Gateway URL
GATEWAY_URL = os.getenv("GATEWAY_URL")