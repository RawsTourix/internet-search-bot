"""
rosbiotech_schedule.py

MCP-сервер для расписания учебных групп РОСБИОТЕХ.

Сервер опирается на rosbiotech_schedule_library_v1.py и предоставляет LLM
только инструменты для работы с группами и расписанием:
- поиск группы;
- расписание на день;
- расписание на неделю;
- обзор учебных дней за период;
- поиск занятий по предмету, преподавателю и типу занятия.

Функций Д/З, материалов и объявлений здесь намеренно нет.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from rosbiotech_schedule_library_v1 import RosbiotechScheduleClient, RosbiotechScheduleError


# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------

LOG_DIR = Path(__file__).resolve().parent / "logging"
LOG_DIR.mkdir(parents=True, exist_ok=True)

formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

server_logger = logging.getLogger("RosbiotechScheduleMCP")
server_logger.setLevel(logging.INFO)

file_handler = RotatingFileHandler(
    filename=LOG_DIR / "rosbiotech_schedule.log",
    maxBytes=8 * 1024 * 1024,
    backupCount=3,
    encoding="utf-8",
)
file_handler.setFormatter(formatter)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)

if not server_logger.handlers:
    server_logger.addHandler(file_handler)
    server_logger.addHandler(console_handler)


# ---------------------------------------------------------------------------
# Инициализация
# ---------------------------------------------------------------------------

mcp = FastMCP(name="rosbiotech-schedule")
client = RosbiotechScheduleClient(logger=server_logger)


async def _run_blocking(func, *args, **kwargs):
    """Выполняет синхронную функцию библиотеки в отдельном потоке."""
    return await asyncio.to_thread(func, *args, **kwargs)


def _handle_error(error: Exception) -> str:
    server_logger.exception("Ошибка инструмента MCP: %s", error)
    return f"Ошибка расписания РОСБИОТЕХ: {error}"


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def find_rosbiotech_groups(query: str, limit: int = 10) -> str:
    """
    Description:
    ---------------
    Найти учебные группы РОСБИОТЕХ по названию, части названия, ID или направлению подготовки.

    Когда использовать:
    ---------------
    Используй этот инструмент, если пользователь называет группу неточно, просит выбрать группу
    или ещё не известен group_id. Для дальнейших инструментов расписания лучше использовать найденный
    group_id, потому что ID однозначнее названия.

    Args:
    ---------------
    query (str): Название группы, часть названия или ID. Примеры: "24о-090301-ИИ/1", "090301", "16675".
    limit (int): Максимальное количество результатов. Рекомендуется 5-10. Значение будет ограничено безопасным диапазоном.

    Returns:
    ---------------
    str: Компактный список найденных групп с названием, id, курсом, формой обучения и направлением.

    Examples:
    ---------------
    Tool call:
    {
      "name": "find_rosbiotech_groups",
      "arguments": {
        "query": "24о-090301-ИИ/1",
        "limit": 5
      }
    }
    """
    try:
        limit = max(1, min(int(limit), 50))
        return await _run_blocking(client.format_groups_search, query, limit=limit)
    except Exception as e:
        return _handle_error(e)


@mcp.tool()
async def get_rosbiotech_schedule_day(group: str, day: str = "today", force_refresh: bool = False) -> str:
    """
    Description:
    ---------------
    Получить расписание группы РОСБИОТЕХ на один конкретный день в текстовом формате.

    Когда использовать:
    ---------------
    Подходит для вопросов:
    - "Какие завтра пары?"
    - "Сколько завтра пар?"
    - "К какой завтра паре?"
    - "Какие пары в пятницу?"
    - "Будут ли сегодня пары?"

    В ответе уже есть количество пар и первая пара, поэтому LLM может напрямую ответить
    на вопросы "сколько пар" и "к какой паре" без дополнительного инструмента.

    Args:
    ---------------
    group (str): ID или название группы. Лучше передавать ID, например "16675".
    day (str): День. Поддерживает: "today", "tomorrow", "сегодня", "завтра", день недели
               ("пятница", "friday"), дату "dd.mm.yyyy" или "yyyy-mm-dd".
    force_refresh (bool): True — принудительно обновить расписание из API, если возможно.
                          False — использовать кэш, если он свежий.

    Returns:
    ---------------
    str: Расписание выбранного дня: дата, группа, количество пар, первая пара и список занятий.

    Examples:
    ---------------
    Tool call:
    {
      "name": "get_rosbiotech_schedule_day",
      "arguments": {
        "group": "16675",
        "day": "tomorrow"
      }
    }
    """
    try:
        return await _run_blocking(client.format_day, group, day, force=bool(force_refresh))
    except Exception as e:
        return _handle_error(e)


@mcp.tool()
async def get_rosbiotech_schedule_week(
    group: str,
    week: str = "current",
    include_empty_days: bool = False,
    force_refresh: bool = False,
) -> str:
    """
    Description:
    ---------------
    Получить расписание группы РОСБИОТЕХ на неделю.

    Когда использовать:
    ---------------
    Подходит для вопросов:
    - "Какие дни мы учимся на следующей неделе?"
    - "Какие пары на этой неделе?"
    - "Покажи расписание на неделю."

    Если пользователю нужны только учебные дни, оставь include_empty_days=False.
    Если важно показать и выходные/пустые дни, передай include_empty_days=True.

    Args:
    ---------------
    group (str): ID или название группы. Лучше передавать ID, например "16675".
    week (str): "current" / "текущая", "next" / "следующая" или любая дата внутри нужной недели.
                Например: "next", "13.05.2026", "2026-05-13".
    include_empty_days (bool): Показывать дни без занятий.
    force_refresh (bool): True — принудительно обновить расписание из API, если возможно.

    Returns:
    ---------------
    str: Расписание по дням выбранной недели.

    Examples:
    ---------------
    Tool call:
    {
      "name": "get_rosbiotech_schedule_week",
      "arguments": {
        "group": "16675",
        "week": "next",
        "include_empty_days": false
      }
    }
    """
    try:
        return await _run_blocking(
            client.format_week,
            group,
            week,
            include_empty=bool(include_empty_days),
            force=bool(force_refresh),
        )
    except Exception as e:
        return _handle_error(e)


@mcp.tool()
async def get_rosbiotech_schedule_overview(
    group: str,
    date_from: str = "today",
    date_to: str = "",
    days: int = 14,
    include_empty_days: bool = False,
    force_refresh: bool = False,
) -> str:
    """
    Description:
    ---------------
    Получить краткий обзор учебных дней за период: какие дни есть занятия, сколько пар,
    с какой пары начинается день и какой парой заканчивается.

    Когда использовать:
    ---------------
    Используй этот инструмент, когда пользователю не нужно полное расписание всех пар,
    а нужен обзор периода. Например:
    - "Какие дни мы учимся на следующей неделе?"
    - "В какие дни есть пары в мае?"
    - "Сколько учебных дней в ближайшие две недели?"

    Args:
    ---------------
    group (str): ID или название группы. Лучше передавать ID, например "16675".
    date_from (str): Начало периода: "today", "tomorrow", "dd.mm.yyyy" или "yyyy-mm-dd".
    date_to (str): Конец периода. Если пусто, используется date_from + days - 1.
    days (int): Длина периода в днях, если date_to не задан. Максимум ограничен библиотекой.
    include_empty_days (bool): Показывать дни без занятий.
    force_refresh (bool): True — принудительно обновить расписание из API, если возможно.

    Returns:
    ---------------
    str: Краткий обзор учебных дней выбранного периода.

    Examples:
    ---------------
    Tool call:
    {
      "name": "get_rosbiotech_schedule_overview",
      "arguments": {
        "group": "16675",
        "date_from": "today",
        "days": 14
      }
    }
    """
    try:
        if date_to == "":
            date_to = None
        return await _run_blocking(
            client.format_overview,
            group,
            date_from=date_from or None,
            date_to=date_to,
            days=max(1, int(days)),
            include_empty=bool(include_empty_days),
            force=bool(force_refresh),
        )
    except Exception as e:
        return _handle_error(e)


@mcp.tool()
async def search_rosbiotech_schedule(
    group: str,
    query: str,
    mode: str = "auto",
    date_from: str = "today",
    date_to: str = "",
    lesson_type: str = "",
    only_future: bool = True,
    limit: int = 20,
    force_refresh: bool = False,
) -> str:
    """
    Description:
    ---------------
    Найти занятия в расписании группы по предмету, преподавателю, аудитории и/или типу занятия.

    Когда использовать:
    ---------------
    Это главный инструмент для вопросов вида:
    - "Когда у нас будет высшая математика?"
    - "Когда будут пары с Филипповой?"
    - "Когда будут основы теории автоматического управления?"
    - "Когда экзамен по английскому?"
    - "Когда следующая практика по управлению проектами?"
    - "Когда лекция по метрологии?"
    - "Будут ли сегодня пары с Тимофеевым?"

    Практические подсказки для LLM:
    ---------------
    - Для поиска преподавателя передай mode="teacher".
    - Для поиска предмета передай mode="subject".
    - Если пользователь не уточнил, предмет это или преподаватель, оставь mode="auto".
    - Для практики передай lesson_type="пр".
    - Для лекции передай lesson_type="лек".
    - Для лабораторной передай lesson_type="лаб".
    - Для зачёта передай lesson_type="зач".
    - Для экзамена передай lesson_type="экз" или query="экз английский".
    - Для вопроса "сегодня" поставь date_from="today", date_to="today".
    - Для будущих занятий оставь only_future=True.

    Args:
    ---------------
    group (str): ID или название группы. Лучше передавать ID, например "16675".
    query (str): Поисковый запрос: предмет, часть предмета, фамилия преподавателя или аудитория.
    mode (str): "auto", "subject" или "teacher".
    date_from (str): Начало периода. По умолчанию today.
    date_to (str): Конец периода. Если пусто, поиск идёт до конца загруженного расписания.
    lesson_type (str): Фильтр типа занятия: "лек", "лаб", "пр", "зач", "экз". Можно оставить пустым.
    only_future (bool): True — искать только сегодня и в будущем. False — искать по всему расписанию/периоду.
    limit (int): Сколько найденных занятий показать. Рекомендуется 10-30.
    force_refresh (bool): True — принудительно обновить расписание из API, если возможно.

    Returns:
    ---------------
    str: Список найденных занятий в компактном формате, сгруппированный по датам.

    Examples:
    ---------------
    Tool call:
    {
      "name": "search_rosbiotech_schedule",
      "arguments": {
        "group": "16675",
        "query": "Филиппова",
        "mode": "teacher",
        "only_future": true,
        "limit": 10
      }
    }
    """
    try:
        if date_to == "":
            date_to = None
        return await _run_blocking(
            client.format_search,
            group,
            query=query,
            mode=mode,
            date_from=date_from or None,
            date_to=date_to,
            lesson_type=lesson_type,
            only_future=bool(only_future),
            limit=max(1, min(int(limit), 100)),
            force=bool(force_refresh),
        )
    except Exception as e:
        return _handle_error(e)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    parser = argparse.ArgumentParser(description="Rosbiotech Schedule MCP Server")
    parser.add_argument("--debug", action="store_true", help="Включить подробное логирование")
    args = parser.parse_args()

    if args.debug:
        server_logger.setLevel(logging.DEBUG)
        console_handler.setLevel(logging.DEBUG)

    try:
        server_logger.info("Запуск MCP-сервера расписания РОСБИОТЕХ")
        await mcp.run_stdio_async()
    except KeyboardInterrupt:
        server_logger.info("Сервер остановлен пользователем")
    except Exception as e:
        server_logger.exception("Критическая ошибка MCP-сервера: %s", e)
        sys.exit(1)
    finally:
        server_logger.info("Работа MCP-сервера завершена")


if __name__ == "__main__":
    asyncio.run(main())
