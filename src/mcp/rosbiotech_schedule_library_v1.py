"""
rosbiotech_schedule_library_v1.py

Библиотека для работы с расписанием учебных групп РОСБИОТЕХ.

Назначение:
- получить список групп;
- найти группу по названию или ID;
- загрузить полное расписание группы;
- показать расписание за день, неделю или диапазон дат;
- искать занятия по предмету, преподавателю и типу занятия;
- возвращать данные в компактном текстовом формате, удобном для LLM.

Библиотека намеренно не содержит функций Д/З, материалов и объявлений.
Это слой подкапотной логики для MCP-сервера rosbiotech_schedule.py.
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
DEFAULT_SEARCH_LIMIT = 20
MAX_RANGE_DAYS = 62

WEEKDAY_RU_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
WEEKDAY_RU_FULL = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
WEEKDAY_ALIASES = {
    "понедельник": 0, "пн": 0, "monday": 0, "mon": 0,
    "вторник": 1, "вт": 1, "tuesday": 1, "tue": 1,
    "среда": 2, "ср": 2, "wednesday": 2, "wed": 2,
    "четверг": 3, "чт": 3, "thursday": 3, "thu": 3,
    "пятница": 4, "пт": 4, "friday": 4, "fri": 4,
    "суббота": 5, "сб": 5, "saturday": 5, "sat": 5,
    "воскресенье": 6, "вс": 6, "sunday": 6, "sun": 6,
}


@dataclass
class ScheduleLoadResult:
    lessons: List[dict]
    source: str
    group_id: int
    group_name: str = ""


@dataclass
class DateRange:
    start: date
    end: date


class RosbiotechScheduleError(Exception):
    """Ошибка библиотеки расписания РОСБИОТЕХ."""


# ---------------------------------------------------------------------------
# Базовые утилиты
# ---------------------------------------------------------------------------


def _default_logger() -> logging.Logger:
    logger = logging.getLogger("RosbiotechSchedule")
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


def _normalize_text(value: Any) -> str:
    """Нормализует строку для поиска: нижний регистр, ё->е, без лишних символов."""
    text = str(value or "").lower().replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9/\-\s.]+", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _query_tokens(query: str) -> List[str]:
    return [part for part in _normalize_text(query).split() if part]


def _contains_all_tokens(text: str, query: str) -> bool:
    tokens = _query_tokens(query)
    if not tokens:
        return True
    target = _normalize_text(text)
    return all(token in target for token in tokens)


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
    request = Request(url, headers={"User-Agent": "RosbiotechScheduleMCP/1.0"})
    with urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw)


def _extract_api_list(data: dict, *path: str) -> List[dict]:
    cur: Any = data
    for key in path:
        if not isinstance(cur, dict):
            return []
        cur = cur.get(key)
    return cur if isinstance(cur, list) else []


# ---------------------------------------------------------------------------
# Даты и диапазоны
# ---------------------------------------------------------------------------


def parse_date_query(value: Optional[str], *, today: Optional[date] = None) -> date:
    """Преобразует человекочитаемую дату в date.

    Поддерживает:
    - today / сегодня;
    - tomorrow / завтра;
    - yesterday / вчера;
    - dd.mm.yyyy, dd-mm-yyyy, yyyy-mm-dd;
    - названия дней недели: пятница, friday и т.д. Возвращает ближайший такой день,
      включая сегодняшний, если он совпадает.
    """
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

    # dd.mm.yyyy / dd-mm-yyyy / dd/mm/yyyy
    m = re.fullmatch(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})", raw)
    if m:
        d, mth, yr = map(int, m.groups())
        if yr < 100:
            yr += 2000
        return date(yr, mth, d)

    # yyyy-mm-dd / yyyy.mm.dd / yyyy/mm/dd
    m = re.fullmatch(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", raw)
    if m:
        yr, mth, d = map(int, m.groups())
        return date(yr, mth, d)

    raise RosbiotechScheduleError(
        f"Не удалось распознать дату '{value}'. Используйте today, tomorrow, пятница, dd.mm.yyyy или yyyy-mm-dd."
    )


def week_start_for_query(value: Optional[str], *, today: Optional[date] = None) -> date:
    """Возвращает понедельник недели по запросу: current, next или любая дата."""
    base = today or date.today()
    raw = (value or "current").strip().lower().replace("ё", "е")
    if raw in ("", "current", "this", "this_week", "текущая", "эта", "эта неделя", "сейчас"):
        d = base
    elif raw in ("next", "next_week", "следующая", "следующая неделя"):
        d = base + timedelta(days=7)
    elif raw in ("previous", "prev", "last_week", "прошлая", "прошлая неделя"):
        d = base - timedelta(days=7)
    else:
        d = parse_date_query(raw, today=base)
    return d - timedelta(days=d.weekday())


def parse_date_range(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    *,
    days: Optional[int] = None,
    today: Optional[date] = None,
) -> DateRange:
    """Преобразует date_from/date_to/days в безопасный диапазон дат."""
    base = today or date.today()
    start = parse_date_query(date_from, today=base) if date_from else base
    if date_to:
        end = parse_date_query(date_to, today=base)
    elif days is not None:
        days = max(1, min(int(days), MAX_RANGE_DAYS))
        end = start + timedelta(days=days - 1)
    else:
        end = start
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
            pass
    return None


# ---------------------------------------------------------------------------
# Нормализация занятий и форматирование
# ---------------------------------------------------------------------------


def lesson_icon(typ: str) -> str:
    """Возвращает emoji-иконку по типу занятия."""
    t = (typ or "").lower()
    if any(k in t for k in ["лек"]):
        return "📘"
    if any(k in t for k in ["лаб"]):
        return "📗"
    if any(k in t for k in ["пр"]):
        return "📙"
    if any(k in t for k in ["зач"]):
        return "📒"
    if any(k in t for k in ["экз"]):
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
        if day is None:
            continue
        grouped.setdefault(day, []).append(item)
    return grouped


def lesson_to_text(item: dict) -> str:
    typ = _lesson_type(item)
    subject = _raw_subject(item) or "—"
    start = _start_time(item)
    end = _end_time(item)
    teacher = _teacher(item) or "—"
    audience = _audience(item) or "—"
    number = _lesson_number(item)
    time_part = f"{start}-{end}" if start or end else "—"
    number_line = f"{number} пара" if number else "Пара"
    return (
        f"{number_line}\n"
        f"{lesson_icon(typ)} {subject}\n"
        f"🕒 {time_part}\n"
        f"👤 {teacher}\n"
        f"🏫 {audience}"
    )


def format_day_schedule(day: date, lessons: List[dict], *, group_label: str = "") -> str:
    lessons = dedupe_lessons(lessons)
    title = f"Расписание на {format_date_ru(day)}"
    if group_label:
        title += f"\nГруппа: {group_label}"
    if not lessons:
        return f"{title}\n\nЗанятий нет."
    first = lessons[0]
    first_number = _lesson_number(first)
    first_start = _start_time(first)
    info = [f"Всего пар: {len(lessons)}"]
    if first_number:
        info.append(f"К первой паре: {first_number} пара" + (f" ({first_start})" if first_start else ""))
    body = "\n\n".join(lesson_to_text(item) for item in lessons)
    return f"{title}\n\n" + "\n".join(info) + f"\n\n{body}"


def format_range_schedule(days: Dict[date, List[dict]], start: date, end: date, *, group_label: str = "", include_empty: bool = False) -> str:
    header = f"Расписание за период {format_date_ru(start)} — {format_date_ru(end)}"
    if group_label:
        header += f"\nГруппа: {group_label}"
    parts = [header]
    for day in daterange(start, end):
        lessons = dedupe_lessons(days.get(day, []))
        if not lessons and not include_empty:
            continue
        parts.append(format_day_schedule(day, lessons))
    if len(parts) == 1:
        parts.append("Занятий в этом периоде нет.")
    return "\n\n---\n\n".join(parts)


def format_days_overview(days: Dict[date, List[dict]], start: date, end: date, *, group_label: str = "", include_empty: bool = False) -> str:
    title = f"Обзор учебных дней {format_date_ru(start)} — {format_date_ru(end)}"
    if group_label:
        title += f"\nГруппа: {group_label}"
    lines = [title]
    learning_days = 0
    for day in daterange(start, end):
        lessons = dedupe_lessons(days.get(day, []))
        if lessons:
            learning_days += 1
            first = lessons[0]
            last = lessons[-1]
            lines.append(
                f"• {format_date_ru(day)} — {len(lessons)} пар; "
                f"с {_lesson_number(first)} пары ({_start_time(first) or '—'}) "
                f"по {_lesson_number(last)} пару ({_end_time(last) or '—'})"
            )
        elif include_empty:
            lines.append(f"• {format_date_ru(day)} — занятий нет")
    lines.insert(1, f"Учебных дней: {learning_days}")
    if learning_days == 0:
        lines.append("В выбранном периоде занятий нет.")
    return "\n".join(lines)


def format_search_results(
    lessons: List[dict],
    *,
    query: str = "",
    mode: str = "auto",
    group_label: str = "",
    limit: int = DEFAULT_SEARCH_LIMIT,
) -> str:
    lessons = dedupe_lessons(lessons)
    shown = lessons[: max(1, int(limit))]
    title = "Результаты поиска по расписанию"
    if query:
        title += f": {query}"
    if group_label:
        title += f"\nГруппа: {group_label}"
    if not lessons:
        return f"{title}\n\nНичего не найдено."
    lines = [title, f"Найдено занятий: {len(lessons)}. Показано: {len(shown)}."]
    current_day: Optional[date] = None
    for item in shown:
        day = _date_from_lesson(item)
        if day != current_day:
            current_day = day
            lines.append(f"\n{format_date_ru(day) if day else 'Без даты'}")
        compact = lesson_to_text(item).replace("\n", " | ")
        lines.append(f"• {compact}")
    if len(lessons) > len(shown):
        lines.append(f"\nЕсть ещё {len(lessons) - len(shown)} занятий. Уточните запрос или увеличьте limit.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Клиент API и бизнес-логика
# ---------------------------------------------------------------------------


class RosbiotechScheduleClient:
    """Клиент расписания РОСБИОТЕХ с локальным кэшем.

    Кэш создаётся в папке data рядом с файлом библиотеки:
    - groups.json;
    - Rasp<group_id>.json.
    """

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
        self.data_dir = Path(data_dir) if data_dir else base_dir / "data"
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
        if not force and isinstance(cached, list) and now - self._groups_cache.get("ts", 0) < self.groups_cache_ttl:
            return cached

        if not force and _file_is_fresh(self._groups_path(), self.groups_cache_ttl):
            local = _json_load(self._groups_path())
            groups = _extract_api_list(local or {}, "data", "groups")
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
        groups = _extract_api_list(local or {}, "data", "groups")
        if groups:
            self._groups_cache = {"ts": now, "groups": groups}
            return groups
        return []

    def find_groups(self, query: str, *, limit: int = 10) -> List[dict]:
        groups = self.get_groups()
        q = _normalize_text(query)
        if not q:
            return groups[:limit]
        scored: List[Tuple[int, dict]] = []
        for group in groups:
            name = str(group.get("groupName") or group.get("name") or "")
            gid = str(group.get("groupID") or group.get("id") or group.get("idGroup") or "")
            specialty = str(group.get("specialtyIDAndName") or "")
            hay = _normalize_text(f"{name} {gid} {specialty}")
            if q == _normalize_text(name) or q == gid:
                score = 100
            elif hay.startswith(q):
                score = 80
            elif q in hay:
                score = 60
            elif all(token in hay for token in q.split()):
                score = 40
            else:
                continue
            scored.append((score, group))
        scored.sort(key=lambda pair: (-pair[0], str(pair[1].get("groupName") or "")))
        return [group for _, group in scored[: max(1, min(limit, 50))]]

    def resolve_group(self, group: Union[str, int]) -> Optional[dict]:
        raw = str(group).strip()
        groups = self.get_groups()
        for item in groups:
            gid = str(item.get("groupID") or item.get("id") or item.get("idGroup") or "")
            name = str(item.get("groupName") or item.get("name") or "")
            if raw == gid or _normalize_text(raw) == _normalize_text(name):
                return item
        matches = self.find_groups(raw, limit=1)
        return matches[0] if matches else None

    def group_id_and_name(self, group: Union[str, int]) -> Tuple[int, str]:
        resolved = self.resolve_group(group)
        if resolved:
            gid = int(resolved.get("groupID") or resolved.get("id") or resolved.get("idGroup"))
            name = str(resolved.get("groupName") or resolved.get("name") or gid)
            return gid, name
        try:
            gid = int(str(group).strip())
            return gid, str(gid)
        except Exception:
            raise RosbiotechScheduleError(
                f"Не удалось найти группу '{group}'. Сначала используйте find_groups."
            )

    def load_schedule(self, group: Union[str, int], *, force: bool = False) -> ScheduleLoadResult:
        group_id, group_name = self.group_id_and_name(group)
        now = time.time()
        mem = self._schedule_cache.get(group_id)
        if not force and mem and now - mem.get("ts", 0) < self.schedule_cache_ttl:
            lessons = mem.get("lessons") or []
            return ScheduleLoadResult(lessons=lessons, source="memory", group_id=group_id, group_name=group_name)

        if not force and _file_is_fresh(self._schedule_path(group_id), self.schedule_cache_ttl):
            local = _json_load(self._schedule_path(group_id))
            lessons = _extract_api_list(local or {}, "data", "rasp")
            if isinstance(lessons, list):
                lessons = dedupe_lessons(lessons)
                self._schedule_cache[group_id] = {"ts": now, "lessons": lessons}
                return ScheduleLoadResult(lessons=lessons, source="local", group_id=group_id, group_name=group_name)

        try:
            data = _http_get_json(SCHEDULE_URL, params={"idGroup": group_id}, timeout=self.http_timeout)
            lessons = _extract_api_list(data, "data", "rasp")
            if isinstance(lessons, list):
                _json_save(self._schedule_path(group_id), data)
                lessons = dedupe_lessons(lessons)
                self._schedule_cache[group_id] = {"ts": now, "lessons": lessons}
                return ScheduleLoadResult(lessons=lessons, source="api", group_id=group_id, group_name=group_name)
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            self.logger.warning("Не удалось получить расписание группы %s с API: %s", group_id, e)
        except Exception as e:
            self.logger.exception("Непредвиденная ошибка при получении расписания: %s", e)

        local = _json_load(self._schedule_path(group_id))
        lessons = _extract_api_list(local or {}, "data", "rasp")
        if isinstance(lessons, list):
            lessons = dedupe_lessons(lessons)
            self._schedule_cache[group_id] = {"ts": now, "lessons": lessons}
            return ScheduleLoadResult(lessons=lessons, source="local", group_id=group_id, group_name=group_name)
        return ScheduleLoadResult(lessons=[], source="none", group_id=group_id, group_name=group_name)

    def lessons_for_day(self, group: Union[str, int], day: Union[str, date], *, force: bool = False) -> Tuple[date, List[dict], str]:
        target = day if isinstance(day, date) else parse_date_query(str(day))
        loaded = self.load_schedule(group, force=force)
        lessons = [item for item in loaded.lessons if _date_from_lesson(item) == target]
        return target, dedupe_lessons(lessons), loaded.group_name

    def lessons_for_range(
        self,
        group: Union[str, int],
        start: date,
        end: date,
        *,
        force: bool = False,
    ) -> Tuple[Dict[date, List[dict]], str]:
        loaded = self.load_schedule(group, force=force)
        result: Dict[date, List[dict]] = {}
        for item in loaded.lessons:
            day = _date_from_lesson(item)
            if day is not None and start <= day <= end:
                result.setdefault(day, []).append(item)
        return {day: dedupe_lessons(items) for day, items in result.items()}, loaded.group_name

    def search_lessons(
        self,
        group: Union[str, int],
        *,
        query: str = "",
        mode: str = "auto",
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        lesson_type: str = "",
        only_future: bool = True,
        limit: int = DEFAULT_SEARCH_LIMIT,
        force: bool = False,
    ) -> Tuple[List[dict], str]:
        loaded = self.load_schedule(group, force=force)
        mode_norm = (mode or "auto").strip().lower()
        today = date.today()
        start = parse_date_query(date_from, today=today) if date_from else (today if only_future else date.min)
        end = parse_date_query(date_to, today=today) if date_to else date.max
        if end < start:
            start, end = end, start

        lesson_type_norm = _normalize_text(lesson_type)
        matches: List[dict] = []
        for item in loaded.lessons:
            day = _date_from_lesson(item)
            if day is None or day < start or day > end:
                continue
            if lesson_type_norm:
                typ = _normalize_text(_lesson_type(item))
                subj = _normalize_text(_raw_subject(item))
                if lesson_type_norm not in typ and not subj.startswith(lesson_type_norm):
                    continue
            if query:
                subject = _raw_subject(item)
                teacher = _teacher(item)
                audience = _audience(item)
                if mode_norm in ("subject", "предмет", "discipline"):
                    ok = _contains_all_tokens(subject, query)
                elif mode_norm in ("teacher", "преподаватель", "препод"):
                    ok = _contains_all_tokens(teacher, query)
                else:
                    ok = (
                        _contains_all_tokens(subject, query)
                        or _contains_all_tokens(teacher, query)
                        or _contains_all_tokens(audience, query)
                    )
                if not ok:
                    continue
            matches.append(item)
        return dedupe_lessons(matches)[: max(1, min(int(limit), 100))], loaded.group_name

    # Текстовые фасады для MCP ------------------------------------------------

    def format_groups_search(self, query: str, *, limit: int = 10) -> str:
        matches = self.find_groups(query, limit=limit)
        if not matches:
            return f"Группы по запросу '{query}' не найдены."
        lines = [f"Найдено групп по запросу '{query}': {len(matches)}"]
        for group in matches:
            name = group.get("groupName") or group.get("name") or "—"
            gid = group.get("groupID") or group.get("id") or group.get("idGroup") or "—"
            course = group.get("course") or "—"
            form = group.get("formStud") or "—"
            specialty = group.get("specialtyIDAndName") or "—"
            lines.append(f"• {name} — id {gid}; курс: {course}; форма: {form}; направление: {specialty}")
        return "\n".join(lines)

    def format_day(self, group: Union[str, int], day: str = "today", *, force: bool = False) -> str:
        target, lessons, label = self.lessons_for_day(group, day, force=force)
        return format_day_schedule(target, lessons, group_label=label)

    def format_week(self, group: Union[str, int], week: str = "current", *, include_empty: bool = False, force: bool = False) -> str:
        start = week_start_for_query(week)
        end = start + timedelta(days=6)
        days, label = self.lessons_for_range(group, start, end, force=force)
        return format_range_schedule(days, start, end, group_label=label, include_empty=include_empty)

    def format_range(
        self,
        group: Union[str, int],
        date_from: str,
        date_to: str,
        *,
        include_empty: bool = False,
        force: bool = False,
    ) -> str:
        rng = parse_date_range(date_from, date_to)
        days, label = self.lessons_for_range(group, rng.start, rng.end, force=force)
        return format_range_schedule(days, rng.start, rng.end, group_label=label, include_empty=include_empty)

    def format_overview(
        self,
        group: Union[str, int],
        *,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        days: int = 14,
        include_empty: bool = False,
        force: bool = False,
    ) -> str:
        rng = parse_date_range(date_from, date_to, days=days)
        grouped, label = self.lessons_for_range(group, rng.start, rng.end, force=force)
        return format_days_overview(grouped, rng.start, rng.end, group_label=label, include_empty=include_empty)

    def format_search(
        self,
        group: Union[str, int],
        *,
        query: str,
        mode: str = "auto",
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
        lesson_type: str = "",
        only_future: bool = True,
        limit: int = DEFAULT_SEARCH_LIMIT,
        force: bool = False,
    ) -> str:
        lessons, label = self.search_lessons(
            group,
            query=query,
            mode=mode,
            date_from=date_from,
            date_to=date_to,
            lesson_type=lesson_type,
            only_future=only_future,
            limit=limit,
            force=force,
        )
        return format_search_results(lessons, query=query, mode=mode, group_label=label, limit=limit)


__all__ = [
    "RosbiotechScheduleClient",
    "RosbiotechScheduleError",
    "parse_date_query",
    "week_start_for_query",
    "parse_date_range",
    "format_date_ru",
    "format_day_schedule",
    "format_range_schedule",
    "format_days_overview",
    "format_search_results",
    "lesson_icon",
]
