"""
rosbiotech_schedule.py

MCP-сервер для расписания учебных групп РОСБИОТЕХ.

Сервер опирается на rosbiotech_schedule_library_v2.py и предоставляет LLM
только инструменты для работы с группами и расписанием:
- поиск группы;
- выдача распиания.

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

from mcp.server.fastmcp import FastMCP

from rosbiotech_schedule_library_v2 import RosbiotechScheduleClient


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


mcp = FastMCP(name="rosbiotech-schedule")
client = RosbiotechScheduleClient(logger=server_logger)


async def _run_blocking(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


def _handle_error(error: Exception) -> str:
    server_logger.exception("Ошибка инструмента MCP: %s", error)
    return f"Ошибка расписания РОСБИОТЕХ: {error}"


@mcp.tool()
async def find_rosbiotech_groups(query: str, limit: int = 10) -> str:
    """Найти учебные группы РОСБИОТЕХ по названию и вернуть JSON со списком групп."""
    try:
        limit = max(1, min(int(limit), 50))
        return await _run_blocking(client.find_groups_json, query, limit=limit)
    except Exception as e:
        return _handle_error(e)


@mcp.tool()
async def get_rosbiotech_schedule(group_id: int, date_from: str = "today", date_to: str = "") -> str:
    """Получить расписание группы РОСБИОТЕХ по group_id за дату или диапазон дат."""
    try:
        return await _run_blocking(client.get_schedule_text, group_id, date_from, date_to)
    except Exception as e:
        return _handle_error(e)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Rosbiotech Schedule MCP Server")
    parser.add_argument("--debug", action="store_true", help="Включить подробное логирование")
    args = parser.parse_args()

    if args.debug or os.environ.get("DEBUG") == "1":
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
