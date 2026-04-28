import os
from dotenv import load_dotenv

# Загрузка переменных из .env
load_dotenv()

# Прокси
HTTP_PROXY = os.getenv("HTTP_PROXY")
HTTPS_PROXY = os.getenv("HTTPS_PROXY")

# Конфигурация Yandex Search API
YANDEX_SEARCH_API_KEY = os.getenv("YANDEX_SEARCH_API_KEY")
YANDEX_CLOUD_FOLDER_ID = os.getenv("YANDEX_CLOUD_FOLDER_ID")