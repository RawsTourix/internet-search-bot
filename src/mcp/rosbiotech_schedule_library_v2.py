"""
rosbiotech_schedule_library_v2.py

Минимальная библиотека для MCP-сервера расписания РОСБИОТЕХ.

Основные сценарии:
- найти группу по названию;
- получить расписание группы по id за дату или диапазон дат.

Библиотека хранит локальный кэш в папке file/rosbiotech_schedule рядом с файлом,
если data_dir не указан явно.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_BASE_URL = "https://dekanat.rosbiotech.ru/api"
GROUPS_URL = f"{API_BASE_URL}/groups"
SCHEDULE_URL = f"{API_BASE_URL}/Rasp"

DEFAULT_HTTP_TIMEOUT = 8
DEFAULT_GROUPS_CACHE_TTL = 60 * 60 * 6      # 6 часов
DEFAULT_SCHEDULE_CACHE_TTL = 60 * 15        # 15 минут
MAX_RANGE_DAYS = 62

WEEKDAY_RU_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
WEEKDAY_ALIASES = {
    "понедельник": 0, "пн": 0, "monday": 0, "mon": 0,
    "вторник": 1, "вт": 1, "tuesday": 1, "tue": 1,
    "среда": 2, "ср": 2, "wednesday": 2, "wed": 2,
    "четверг": 3, "чт": 3, "thursday": 3, "thu": 3,
    "пятница": 4, "пт": 4, "friday": 4, "fri": 4,
    "суббота": 5, "сб": 5, "saturday": 5, "sat": 5,
    "воскресенье": 6, "вс": 6, "sunday": 6, "sun": 6,
}


class RosbiotechScheduleError(Exception):
    """Ошибка библиотеки расписания РОСБИОТЕХ."""


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date


@dataclass
class ScheduleLoadResult:
    lessons: List[dict]
    source: str
    group_id: int
    group_name: str = ""


# ---------------------------------------------------------------------------
# Базовые утилиты
# ---------------------------------------------------------------------------


def _default_logger() -> logging.Logger:
    logger = logging.getLogger("RosbiotechSchedule")
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


def _normalize_text(value: Any) -> str:
    """Нормализует строку для поиска."""
    text = str(value or "").lower().replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9/\-\s.]+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _json_load(path: Path) -> Optional[dict]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return None


def _json_save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _file_is_fresh(path: Path, ttl: int) -> bool:
    try:
        return path.exists() and (time.time() - path.stat().st_mtime) < ttl
    except Exception:
        return False


def _http_get_json(url: str, params: Optional[Dict[str, Any]] = None, timeout: int = DEFAULT_HTTP_TIMEOUT) -> dict:
    if params:
        url = f"{url}?{urlencode(params)}"
    request = Request(url, headers={"User-Agent": "RosbiotechScheduleMCP/2.0"})
    with urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw)


def _extract_api_list(data: Optional[dict], *path: str) -> List[dict]:
    cur: Any = data or {}
    for key in path:
        if not isinstance(cur, dict):
            return []
        cur = cur.get(key)
    return cur if isinstance(cur, list) else []


def _group_name(group: dict) -> str:
    return str(group.get("groupName") or group.get("name") or "").strip()


def _group_id(group: dict) -> Optional[int]:
    raw = group.get("groupID") or group.get("id") or group.get("idGroup")
    try:
        return int(raw)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Даты
# ---------------------------------------------------------------------------


def parse_date(value: Optional[str], *, today: Optional[date] = None) -> date:
    """Преобразует today/tomorrow/день недели/dd.mm.yyyy/yyyy-mm-dd в date."""
    base = today or date.today()
    raw = (value or "today").strip().lower().replace("ё", "е")

    if raw in ("", "today", "сегодня", "now"):
        return base
    if raw in ("tomorrow", "завтра"):
        return base + timedelta(days=1)
    if raw in ("yesterday", "вчера"):
        return base - timedelta(days=1)

    weekday = WEEKDAY_ALIASES.get(raw)
    if weekday is not None:
        delta = (weekday - base.weekday()) % 7
        return base + timedelta(days=delta)

    m = re.fullmatch(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})", raw)
    if m:
        day, month, year = map(int, m.groups())
        if year < 100:
            year += 2000
        return date(year, month, day)

    m = re.fullmatch(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", raw)
    if m:
        year, month, day = map(int, m.groups())
        return date(year, month, day)

    raise RosbiotechScheduleError(
        f"Не удалось распознать дату '{value}'. Используйте YYYY-MM-DD, DD.MM.YYYY, today, tomorrow или день недели."
    )


def parse_date_range(date_from: Optional[str] = None, date_to: Optional[str] = None) -> DateRange:
    start = parse_date(date_from or "today")
    end = parse_date(date_to) if date_to else start
    if end < start:
        start, end = end, start
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        end = start + timedelta(days=MAX_RANGE_DAYS - 1)
    return DateRange(start=start, end=end)


def daterange(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def format_date_ru(day: date) -> str:
    return f"{day.strftime('%d.%m.%Y')} ({WEEKDAY_RU_SHORT[day.weekday()]})"


def _date_from_lesson(item: dict) -> Optional[date]:
    value = item.get("дата") or item.get("date") or item.get("датаНачала") or item.get("dateStart")
    if not value:
        return None
    if isinstance(value, str):
        value = value.split("T", 1)[0]
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Занятия и форматирование
# ---------------------------------------------------------------------------


def lesson_icon(lesson_type_or_subject: str) -> str:
    text = (lesson_type_or_subject or "").lower()
    if "лек" in text:
        return "📘"
    if "лаб" in text:
        return "📗"
    if "пр" in text:
        return "📙"
    if "зач" in text:
        return "📒"
    if "экз" in text or "экзам" in text:
        return "📕"
    return "📓"


def _raw_subject(item: dict) -> str:
    return str(item.get("дисциплина") or item.get("discipline") or item.get("предмет") or "").strip()


def _lesson_type(item: dict) -> str:
    subject = _raw_subject(item)
    first = subject.split(maxsplit=1)[0] if subject else ""
    return str(item.get("тип") or item.get("type") or first or "").strip()


def _teacher(item: dict) -> str:
    return str(item.get("преподаватель") or item.get("teacher") or item.get("фиоПреподавателя") or "").strip()


def _audience(item: dict) -> str:
    return str(item.get("аудитория") or item.get("audience") or item.get("ауд") or "").strip()


def _lesson_number(item: dict) -> int:
    value = item.get("номерЗанятия") or item.get("номерПары") or item.get("lessonNumber") or 0
    try:
        return int(value)
    except Exception:
        return 0


def _start_time(item: dict) -> str:
    return str(item.get("начало") or item.get("start") or item.get("timeStart") or "").strip()


def _end_time(item: dict) -> str:
    return str(item.get("конец") or item.get("end") or item.get("timeEnd") or "").strip()


def _lesson_key(item: dict) -> Tuple[Any, ...]:
    day = _date_from_lesson(item) or date.min
    return (
        day.isoformat(),
        _lesson_number(item),
        _start_time(item),
        _end_time(item),
        _raw_subject(item),
        _teacher(item),
        _audience(item),
    )


def dedupe_lessons(lessons: Iterable[dict]) -> List[dict]:
    unique: List[dict] = []
    seen: set = set()
    for item in lessons or []:
        key = _lesson_key(item)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    unique.sort(key=_lesson_key)
    return unique


def group_lessons_by_date(lessons: Iterable[dict]) -> Dict[date, List[dict]]:
    grouped: Dict[date, List[dict]] = {}
    for item in dedupe_lessons(lessons):
        day = _date_from_lesson(item)
        if day is not None:
            grouped.setdefault(day, []).append(item)
    return grouped


def lesson_to_text(item: dict) -> str:
    subject = _raw_subject(item) or "—"
    lesson_type = _lesson_type(item) or subject
    teacher = _teacher(item) or "—"
    audience = _audience(item) or "—"
    start = _start_time(item)
    end = _end_time(item)
    time_part = f"{start}-{end}" if start or end else "—"
    number = _lesson_number(item)
    number_line = f"{number} пара" if number else "Пара"

    return (
        f"{number_line}\n"
        f"{lesson_icon(lesson_type or subject)} {subject}\n"
        f"🕒 {time_part}\n"
        f"👤 {teacher}\n"
        f"🏫 {audience}"
    )


def format_day_schedule(day: date, lessons: List[dict]) -> str:
    lessons = dedupe_lessons(lessons)
    title = f"Расписание на {format_date_ru(day)}"
    if not lessons:
        return f"{title}\nНет пар."
    return f"{title}\n\n" + "\n\n".join(lesson_to_text(item) for item in lessons)


def format_schedule_range(
    lessons: List[dict],
    start: date,
    end: date,
    *,
    group_name: str = "",
) -> str:
    grouped = group_lessons_by_date(lessons)
    is_single_day = start == end

    if is_single_day:
        return format_day_schedule(start, grouped.get(start, []))

    parts: List[str] = []
    if group_name:
        parts.append(f"Группа: {group_name}")

    for day in daterange(start, end):
        parts.append(format_day_schedule(day, grouped.get(day, [])))

    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Клиент РОСБИОТЕХ API
# ---------------------------------------------------------------------------


class RosbiotechScheduleClient:
    """Клиент API расписания РОСБИОТЕХ с локальным кэшем."""

    def __init__(
        self,
        data_dir: Optional[Union[str, Path]] = None,
        *,
        http_timeout: int = DEFAULT_HTTP_TIMEOUT,
        groups_cache_ttl: int = DEFAULT_GROUPS_CACHE_TTL,
        schedule_cache_ttl: int = DEFAULT_SCHEDULE_CACHE_TTL,
        logger: Optional[logging.Logger] = None,
    ):
        base_dir = Path(__file__).resolve().parent
        self.data_dir = Path(data_dir) if data_dir else base_dir / "file" / "rosbiotech_schedule"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.http_timeout = http_timeout
        self.groups_cache_ttl = groups_cache_ttl
        self.schedule_cache_ttl = schedule_cache_ttl
        self.logger = logger or _default_logger()
        self._groups_cache: Dict[str, Any] = {}
        self._schedule_cache: Dict[int, Dict[str, Any]] = {}

    def _groups_path(self) -> Path:
        return self.data_dir / "groups.json"

    def _schedule_path(self, group_id: int) -> Path:
        return self.data_dir / f"Rasp{int(group_id)}.json"

    def get_groups(self, *, force: bool = False) -> List[dict]:
        now = time.time()
        cached = self._groups_cache.get("groups")
        if not force and isinstance(cached, list) and now - float(self._groups_cache.get("ts", 0)) < self.groups_cache_ttl:
            return cached

        if not force and _file_is_fresh(self._groups_path(), self.groups_cache_ttl):
            local = _json_load(self._groups_path())
            groups = _extract_api_list(local, "data", "groups")
            if groups:
                self._groups_cache = {"ts": now, "groups": groups}
                return groups

        try:
            data = _http_get_json(GROUPS_URL, timeout=self.http_timeout)
            groups = _extract_api_list(data, "data", "groups")
            if groups:
                _json_save(self._groups_path(), data)
                self._groups_cache = {"ts": now, "groups": groups}
                return groups
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            self.logger.warning("Не удалось получить список групп с API: %s", e)
        except Exception as e:
            self.logger.exception("Непредвиденная ошибка при получении групп: %s", e)

        local = _json_load(self._groups_path())
        groups = _extract_api_list(local, "data", "groups")
        if groups:
            self._groups_cache = {"ts": now, "groups": groups}
            return groups
        return []

    def find_groups(self, query: str, *, limit: int = 10) -> List[dict]:
        groups = self.get_groups()
        q = _normalize_text(query)
        limit = max(1, min(int(limit), 50))
        if not q:
            return groups[:limit]

        scored: List[Tuple[int, str, dict]] = []
        for group in groups:
            name = _group_name(group)
            specialty = str(group.get("specialtyIDAndName") or "")
            haystack = _normalize_text(f"{name} {specialty}")
            normalized_name = _normalize_text(name)

            if q == normalized_name:
                score = 100
            elif normalized_name.startswith(q):
                score = 80
            elif q in haystack:
                score = 60
            elif all(token in haystack for token in q.split()):
                score = 40
            else:
                continue

            scored.append((score, name, group))

        scored.sort(key=lambda item: (-item[0], item[1]))
        return [group for _, __, group in scored[:limit]]

    def find_groups_payload(self, query: str, *, limit: int = 10) -> dict:
        matches = self.find_groups(query, limit=limit)
        groups = []
        for group in matches:
            gid = _group_id(group)
            name = _group_name(group)
            if gid is not None and name:
                groups.append({"name": name, "id": gid})

        status = "ok" if groups else "not_found"
        return {"status": status, "query": query, "groups": groups}

    def find_groups_json(self, query: str, *, limit: int = 10) -> str:
        return json.dumps(self.find_groups_payload(query, limit=limit), ensure_ascii=False)

    def resolve_group_id(self, group_id: Union[int, str]) -> Tuple[int, str]:
        try:
            gid = int(str(group_id).strip())
        except Exception:
            raise RosbiotechScheduleError("group_id должен быть числом из результата find_rosbiotech_groups.")

        groups = self.get_groups()
        for group in groups:
            if _group_id(group) == gid:
                return gid, _group_name(group) or str(gid)
        return gid, str(gid)

    def _load_local_schedule(self, group_id: int) -> Optional[ScheduleLoadResult]:
        local = _json_load(self._schedule_path(group_id))
        lessons = _extract_api_list(local, "data", "rasp")
        if isinstance(lessons, list):
            meta = local.get("_meta", {}) if isinstance(local, dict) else {}
            group_name = str(meta.get("group_name") or "")
            return ScheduleLoadResult(
                lessons=dedupe_lessons(lessons),
                source="local",
                group_id=group_id,
                group_name=group_name,
            )
        return None

    def _save_schedule(self, group_id: int, lessons: List[dict], *, group_name: str = "", source: str = "api") -> None:
        payload = {
            "data": {"rasp": dedupe_lessons(lessons)},
            "_meta": {
                "source": source,
                "group_id": int(group_id),
                "group_name": group_name,
                "updated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            },
        }
        _json_save(self._schedule_path(group_id), payload)

    def _fetch_full_schedule(self, group_id: int, *, group_name: str = "") -> Optional[ScheduleLoadResult]:
        data = _http_get_json(SCHEDULE_URL, params={"idGroup": group_id}, timeout=self.http_timeout)
        lessons = _extract_api_list(data, "data", "rasp")
        if not isinstance(lessons, list):
            return None
        lessons = dedupe_lessons(lessons)
        self._save_schedule(group_id, lessons, group_name=group_name, source="api_full")
        return ScheduleLoadResult(lessons=lessons, source="api", group_id=group_id, group_name=group_name)

    def _fetch_schedule_by_sdate(self, group_id: int, target: date, *, group_name: str = "") -> Optional[ScheduleLoadResult]:
        data = _http_get_json(
            SCHEDULE_URL,
            params={"idGroup": group_id, "sdate": target.isoformat()},
            timeout=self.http_timeout,
        )
        lessons = _extract_api_list(data, "data", "rasp")
        if not isinstance(lessons, list):
            return None
        lessons = dedupe_lessons(lessons)
        return ScheduleLoadResult(lessons=lessons, source="api_sdate", group_id=group_id, group_name=group_name)

    def load_schedule(self, group_id: Union[int, str], *, force: bool = False) -> ScheduleLoadResult:
        gid, group_name = self.resolve_group_id(group_id)
        now = time.time()

        mem = self._schedule_cache.get(gid)
        if not force and mem and now - float(mem.get("ts", 0)) < self.schedule_cache_ttl:
            return ScheduleLoadResult(
                lessons=mem.get("lessons") or [],
                source="memory",
                group_id=gid,
                group_name=group_name,
            )

        if not force and _file_is_fresh(self._schedule_path(gid), self.schedule_cache_ttl):
            local = self._load_local_schedule(gid)
            if local is not None:
                if not local.group_name:
                    local.group_name = group_name
                self._schedule_cache[gid] = {"ts": now, "lessons": local.lessons}
                return local

        try:
            loaded = self._fetch_full_schedule(gid, group_name=group_name)
            if loaded is not None:
                self._schedule_cache[gid] = {"ts": now, "lessons": loaded.lessons}
                return loaded
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            self.logger.warning("Не удалось получить полное расписание группы %s с API: %s", gid, e)
        except Exception as e:
            self.logger.exception("Непредвиденная ошибка при получении расписания группы %s: %s", gid, e)

        local = self._load_local_schedule(gid)
        if local is not None:
            if not local.group_name:
                local.group_name = group_name
            self._schedule_cache[gid] = {"ts": now, "lessons": local.lessons}
            return local

        return ScheduleLoadResult(lessons=[], source="none", group_id=gid, group_name=group_name)

    def get_schedule_text(self, group_id: Union[int, str], date_from: str = "today", date_to: str = "") -> str:
        rng = parse_date_range(date_from, date_to)
        loaded = self.load_schedule(group_id)

        # Если полной/локальной копии нет, пробуем точечный API sdate как резерв.
        if not loaded.lessons and (rng.end - rng.start).days <= 14:
            collected: List[dict] = []
            for day in daterange(rng.start, rng.end):
                try:
                    chunk = self._fetch_schedule_by_sdate(loaded.group_id, day, group_name=loaded.group_name)
                    if chunk and chunk.lessons:
                        collected.extend(chunk.lessons)
                except Exception as e:
                    self.logger.warning("Не удалось получить расписание группы %s по sdate=%s: %s", loaded.group_id, day, e)
            if collected:
                loaded.lessons = dedupe_lessons(collected)
                loaded.source = "api_sdate"
                self._save_schedule(loaded.group_id, loaded.lessons, group_name=loaded.group_name, source="api_sdate")

        lessons = [
            item for item in loaded.lessons
            if (day := _date_from_lesson(item)) is not None and rng.start <= day <= rng.end
        ]
        return format_schedule_range(lessons, rng.start, rng.end, group_name=loaded.group_name)


__all__ = [
    "RosbiotechScheduleClient",
    "RosbiotechScheduleError",
    "parse_date",
    "parse_date_range",
    "format_date_ru",
    "format_day_schedule",
    "format_schedule_range",
    "lesson_icon",
]