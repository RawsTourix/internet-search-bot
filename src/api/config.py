import logging
import os
from dotenv import load_dotenv

# Загрузка переменных из .env
load_dotenv()

# Прокси
HTTP_PROXY = os.getenv("HTTP_PROXY", "")
HTTPS_PROXY = os.getenv("HTTPS_PROXY", "")

# Путь к настройкам ботов
AGENT_CONFIG_PATH = os.getenv("AGENT_CONFIG_PATH", "")