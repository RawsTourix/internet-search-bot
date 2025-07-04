import os
import sys
import argparse
import logging
import asyncio
from typing import List, Dict, Any, Union, Optional
from logging.handlers import RotatingFileHandler
from mcp.server.fastmcp import FastMCP

from yandex_search import YandexSearchAPI, format_results, optimize_results

# Импорт модулей
from config import HTTP_PROXY, HTTPS_PROXY, YANDEX_SEARCH_API_KEY, YANDEX_CLOUD_FOLDER_ID

# Настройка прокси
os.environ['http_proxy'] = HTTP_PROXY
os.environ['https_proxy'] = HTTPS_PROXY

# Проверяем и создаем папку для логов
log_dir = "logging"
if not os.path.exists(log_dir):
    os.makedirs(log_dir, exist_ok=True)

# Настройка логирования
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

# Логгер для MainBot
main_logger = logging.getLogger("MainBot")
main_logger.setLevel(logging.DEBUG)

main_file_handler = RotatingFileHandler(
    filename=os.path.join(log_dir, "main_bot.log"),
    maxBytes=8*1024*1024,  # 8 MB
    encoding='utf-8'
)
main_file_handler.setFormatter(formatter)
main_logger.addHandler(main_file_handler)

main_console_handler = logging.StreamHandler()
main_console_handler.setLevel(logging.INFO)
main_console_handler.setFormatter(formatter)

main_logger.addHandler(main_file_handler)
main_logger.addHandler(main_console_handler)

# Логгер для YandexSearch
yc_logger = logging.getLogger("YandexSearch")
yc_logger.setLevel(logging.DEBUG)

yc_file_handler = RotatingFileHandler(
    filename=os.path.join(log_dir, "yandex_search.log"),
    maxBytes=8*1024*1024,  # 8 MB
    encoding='utf-8'
)
yc_file_handler.setFormatter(formatter)

yc_console_handler = logging.StreamHandler()
yc_console_handler.setLevel(logging.INFO)
yc_console_handler.setFormatter(formatter)

yc_logger.addHandler(yc_file_handler)
yc_logger.addHandler(yc_console_handler)

# Инициализация Yandex Search клиента
client = YandexSearchAPI(api_key=YANDEX_SEARCH_API_KEY, folder_id=YANDEX_CLOUD_FOLDER_ID, logger=yc_logger)

# Инициализация сервера
mcp = FastMCP(name="main-bot")

######################################
## ОСНОВНЫЕ ФУНКЦИИ С ИНСТРУМЕНТАМИ ##
######################################

@mcp.tool()
async def search_internet(
    query: str,
    num: int = 5,
) -> str:
    """
    Description:
    ---------------
        Поиск в интернете
    Args:
    ---------------
        query (str): Поисковый запрос
        num (int): Количество результатов поиска (от 1 до 10)
    Returns:
    ---------------
        str: Результаты поиска
    Examples:
    ---------------
        Tool call:
        {
            "name": "search",
            "arguments": {
                "query": "новости искусственного интеллекта",
                "num": 5
            }
        }
    """
    try:
        # Предварительная проверка
        if not query:
            raise ValueError("Запрос не может быть пустым")
        
        if num > 10: num = 10
        if num < 1: num = 1


        # Поиск результатов
        results = await client.search(
            query_text=query,
            groups_on_page=num
        )

        # Оптимизация данных
        optimized_results = optimize_results(
            parsed_results=results,
            min_length=30
        )

        # Возврат в понятном формате
        return format_results(optimized_results, query)

    except Exception as e:
        return f"Ошибка поиска: {e}"

async def main() -> None:
    """Основная точка входа"""
    parser = argparse.ArgumentParser(description="Main Bot Server")
    parser.add_argument("--debug", action="store_true", help="Включить подробное логирование")
    args = parser.parse_args()

    # Установка уровня логирования
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    try:
        main_logger.info("Запуск главного бота")
        await mcp.run_stdio_async()
    except KeyboardInterrupt:
        main_logger.info("Сервер остановлен пользователем")
    except Exception as e:
        main_logger.exception(f"Критическая ошибка: {e}")
        sys.exit(1)
    finally:
        main_logger.info("Работа сервера завершена")

if __name__ == "__main__":
    asyncio.run(main())