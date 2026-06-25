import os
import re
import sys
import argparse
import logging
import asyncio
from typing import List, Dict, Any, Union, Optional
from logging.handlers import RotatingFileHandler
from mcp.server.fastmcp import FastMCP

from yandex_search_library import YandexSearchAPI, format_results, optimize_results

# Импорт модулей
from yandex_search_config import HTTP_PROXY, HTTPS_PROXY, YANDEX_SEARCH_API_KEY, YANDEX_CLOUD_FOLDER_ID

# Настройка прокси
os.environ['http_proxy'] = HTTP_PROXY
os.environ['https_proxy'] = HTTPS_PROXY

# Проверяем и создаем папку для логов
log_dir = "logging"
if not os.path.exists(log_dir):
    os.makedirs(log_dir, exist_ok=True)

# Настройка логирования
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

# Логгер для YandexSearch
main_logger = logging.getLogger("YandexSearch")
main_logger.setLevel(logging.DEBUG)

main_file_handler = RotatingFileHandler(
    filename=os.path.join(log_dir, "yandex_search.log"),
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
mcp = FastMCP(name="yandex-search")

######################################
## ОСНОВНЫЕ ФУНКЦИИ С ИНСТРУМЕНТАМИ ##
######################################

def _clamp_int(value: int, minimum: int, maximum: int) -> int:
    try:
        value = int(value)
    except Exception:
        value = minimum

    return max(minimum, min(maximum, value))


def _has_cyrillic(text: str) -> bool:
    return bool(re.search(r"[А-Яа-яЁё]", text or ""))


def _has_latin(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", text or ""))


def _normalize_search_type(search_type: str, query: str) -> str:
    """
    Yandex Search API:
    - SEARCH_TYPE_RU  — русский поиск
    - SEARCH_TYPE_COM — международный поиск
    """
    value = (search_type or "auto").strip().lower()

    aliases = {
        "ru": "SEARCH_TYPE_RU",
        "russian": "SEARCH_TYPE_RU",
        "русский": "SEARCH_TYPE_RU",
        "com": "SEARCH_TYPE_COM",
        "international": "SEARCH_TYPE_COM",
        "global": "SEARCH_TYPE_COM",
        "en": "SEARCH_TYPE_COM",
        "english": "SEARCH_TYPE_COM",
        "world": "SEARCH_TYPE_COM",
    }

    if value in aliases:
        return aliases[value]

    if value.startswith("search_type_"):
        return value.upper()

    # auto: если запрос явно английский/международный — COM, иначе RU
    if _has_latin(query) and not _has_cyrillic(query):
        return "SEARCH_TYPE_COM"

    return "SEARCH_TYPE_RU"


def _normalize_l10n(l10n: str, search_type: str) -> str:
    value = (l10n or "auto").strip().lower()

    aliases = {
        "ru": "LOCALIZATION_RU",
        "russian": "LOCALIZATION_RU",
        "русский": "LOCALIZATION_RU",
        "en": "LOCALIZATION_EN",
        "english": "LOCALIZATION_EN",
        "com": "LOCALIZATION_EN",
        "international": "LOCALIZATION_EN",
    }

    if value in aliases:
        return aliases[value]

    if value.startswith("localization_"):
        return value.upper()

    if search_type == "SEARCH_TYPE_COM":
        return "LOCALIZATION_EN"

    return "LOCALIZATION_RU"


def _normalize_sort_mode(sort_mode: str) -> str:
    value = (sort_mode or "relevance").strip().lower()

    aliases = {
        "relevance": "SORT_MODE_BY_RELEVANCE",
        "rel": "SORT_MODE_BY_RELEVANCE",
        "по релевантности": "SORT_MODE_BY_RELEVANCE",
        "time": "SORT_MODE_BY_TIME",
        "date": "SORT_MODE_BY_TIME",
        "fresh": "SORT_MODE_BY_TIME",
        "recent": "SORT_MODE_BY_TIME",
        "news": "SORT_MODE_BY_TIME",
        "по времени": "SORT_MODE_BY_TIME",
    }

    if value in aliases:
        return aliases[value]

    if value.startswith("sort_mode_"):
        return value.upper()

    return "SORT_MODE_BY_RELEVANCE"


async def _run_yandex_search(
    query: str,
    results: int = 5,
    search_type: str = "auto",
    sort_mode: str = "relevance",
    l10n: str = "auto",
    pages: int = 1,
    max_passages: int = 5,
    min_length: int = 30,
) -> str:
    if not query or not query.strip():
        raise ValueError("Запрос не может быть пустым")

    results = _clamp_int(results, 1, 10)
    pages = _clamp_int(pages, 1, 3)
    max_passages = _clamp_int(max_passages, 1, 5)

    normalized_search_type = _normalize_search_type(search_type, query)
    normalized_l10n = _normalize_l10n(l10n, normalized_search_type)
    normalized_sort_mode = _normalize_sort_mode(sort_mode)

    parsed_results = await client.search(
        query_text=query.strip(),
        groups_on_page=results,
        pages_to_fetch=list(range(pages)),
        max_passages=max_passages,
        search_type=normalized_search_type,
        sort_mode=normalized_sort_mode,
        sort_order="SORT_ORDER_DESC",
        l10n=normalized_l10n,
    )

    optimized_results = optimize_results(
        parsed_results=parsed_results,
        min_length=min_length,
    )

    return format_results(optimized_results, query)


@mcp.tool()
async def search_internet(
    query: str,
    results: int = 5,
    search_type: str = "auto",
    sort_mode: str = "relevance",
    l10n: str = "auto",
    pages: int = 1,
    max_passages: int = 5,
) -> str:
    """Настраиваемый веб-поиск: query, results=1..10, search_type=auto|ru|com, sort_mode=relevance|time, l10n=auto|ru|en, pages=1..3, max_passages=1..5."""
    try:
        return await _run_yandex_search(
            query=query,
            results=results,
            search_type=search_type,
            sort_mode=sort_mode,
            l10n=l10n,
            pages=pages,
            max_passages=max_passages,
        )

    except Exception as e:
        return f"Ошибка поиска: {e}"


@mcp.tool()
async def search_news(
    query: str,
    results: int = 10,
    search_type: str = "auto",
    l10n: str = "auto",
    pages: int = 1,
    max_passages: int = 5,
) -> str:
    """Поиск свежих новостей и текущих событий с сортировкой по времени."""
    try:
        return await _run_yandex_search(
            query=query,
            results=results,
            search_type=search_type,
            sort_mode="time",
            l10n=l10n,
            pages=pages,
            max_passages=max_passages,
        )

    except Exception as e:
        return f"Ошибка поиска новостей: {e}"
    
@mcp.tool()
async def search_url(
    query: str,
    results: int = 5,
    search_type: str = "auto",
    l10n: str = "auto",
) -> str:
    """Быстрый поиск URL (официальный сайт, документация, GitHub, страница товара и т.п.)."""
    try:
        return await _run_yandex_search(
            query=query,
            results=results,
            search_type=search_type,
            sort_mode="relevance",
            l10n=l10n,
            pages=1,
            max_passages=1,
            min_length=0,
        )

    except Exception as e:
        return f"Ошибка поиска URL: {e}"

async def main() -> None:
    """Основная точка входа"""
    parser = argparse.ArgumentParser(description="Yandex Search MCP Server")
    parser.add_argument("--debug", action="store_true", help="Включить подробное логирование")
    args = parser.parse_args()

    # Установка уровня логирования
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    try:
        main_logger.info("Запуск MCP-сервера поиска в интернете")
        await mcp.run_stdio_async()
    except KeyboardInterrupt:
        main_logger.info("Сервер остановлен пользователем")
    except Exception as e:
        main_logger.exception(f"Критическая ошибка MCP-сервера: {e}")
        sys.exit(1)
    finally:
        main_logger.info("Работа MCP-сервера завершена")

if __name__ == "__main__":
    asyncio.run(main())