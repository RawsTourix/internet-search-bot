"""Artifact-domain progress localization registered without transport coupling."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from ..agent.progress_messages import PROGRESS_MESSAGES


ARTIFACT_PROGRESS_FILENAME_PREVIEW_MAX_ITEMS = 3
ARTIFACT_PROGRESS_FILENAME_MAX_CHARS = 80


_ARTIFACT_PROGRESS_MESSAGES = {
    "ru": {
        "artifact_candidate_list": "📦 Проверяю доступные результаты файловой обработки…",
        "artifact_create_from_content": "📦 Сохраняю результат обработки как файл…",
        "artifact_create_version_from_content": "📦 Создаю новую версию из результата обработки…",
        # Compatibility keys remain singular for older direct callers.
        "artifact_set_delivery": "📤 Выбираю файл для отправки…",
        "artifact_set_delivery_one": "📤 Выбираю файл для отправки…",
        "artifact_set_delivery_many": "📤 Выбираю файлы для отправки ({file_count})…",
        "artifact_cancel_delivery_one": "⛔ Исключаю файл из отправки…",
        "artifact_cancel_delivery_many": "⛔ Исключаю файлы из отправки ({file_count})…",
        "artifact_ingested": "✅ Входной файл сохранён: {filename}",
        "artifact_delivery_selected": "📤 Файл выбран для отправки: {filename}",
        "artifact_delivery_selected_one": "📤 Файл выбран для отправки: {filename}",
        "artifact_delivery_selected_many": (
            "📤 Для отправки выбраны файлы ({file_count}): {filenames_preview}"
        ),
        "artifact_delivery_cancelled": "⛔ Файл исключён из отправки: {filename}",
        "artifact_delivery_cancelled_one": "⛔ Файл исключён из отправки: {filename}",
        "artifact_delivery_cancelled_many": (
            "⛔ Из отправки исключены файлы ({file_count}): {filenames_preview}"
        ),
        "artifact_delivery_started": "📤 Отправляю файл: {filename}",
        "artifact_delivery_done": "✅ Файл отправлен: {filename}",
        "artifact_delivery_failed": "⚠️ Не удалось отправить файл: {filename}",
        "artifact_delivery_unknown": "⚠️ Результат отправки файла неизвестен: {filename}",
        "artifact_read_completed": "📖 Чтение файлов завершено.",
    },
    "en": {
        "artifact_candidate_list": "📦 Checking available file-processing results…",
        "artifact_create_from_content": "📦 Saving the processing result as a file…",
        "artifact_create_version_from_content": "📦 Creating a new version from the processing result…",
        # Compatibility keys remain singular for older direct callers.
        "artifact_set_delivery": "📤 Selecting a file for delivery…",
        "artifact_set_delivery_one": "📤 Selecting a file for delivery…",
        "artifact_set_delivery_many": "📤 Selecting files for delivery ({file_count})…",
        "artifact_cancel_delivery_one": "⛔ Removing a file from delivery…",
        "artifact_cancel_delivery_many": "⛔ Removing files from delivery ({file_count})…",
        "artifact_ingested": "✅ Input file stored: {filename}",
        "artifact_delivery_selected": "📤 File selected for delivery: {filename}",
        "artifact_delivery_selected_one": "📤 File selected for delivery: {filename}",
        "artifact_delivery_selected_many": (
            "📤 Files selected for delivery ({file_count}): {filenames_preview}"
        ),
        "artifact_delivery_cancelled": "⛔ File removed from delivery: {filename}",
        "artifact_delivery_cancelled_one": "⛔ File removed from delivery: {filename}",
        "artifact_delivery_cancelled_many": (
            "⛔ Files removed from delivery ({file_count}): {filenames_preview}"
        ),
        "artifact_delivery_started": "📤 Sending file: {filename}",
        "artifact_delivery_done": "✅ File sent: {filename}",
        "artifact_delivery_failed": "⚠️ Failed to send file: {filename}",
        "artifact_delivery_unknown": "⚠️ File delivery result is unknown: {filename}",
        "artifact_read_completed": "📖 File reading completed.",
    },
}


def artifact_delivery_start_message_key(*, selected: bool, count: int) -> str:
    """Return the localized progress key for one aggregate tool invocation."""

    cardinality = "one" if count == 1 else "many"
    operation = "artifact_set_delivery" if selected else "artifact_cancel_delivery"
    return f"{operation}_{cardinality}"


def artifact_delivery_event_message_key(event_type: str, *, count: int) -> str:
    """Choose a cardinality-aware key without changing the event type."""

    if event_type not in {
        "artifact_delivery_selected",
        "artifact_delivery_cancelled",
    }:
        return event_type
    cardinality = "one" if count == 1 else "many"
    return f"{event_type}_{cardinality}"


def artifact_delivery_message_projection(
    filenames: Sequence[Any],
) -> dict[str, Any]:
    """Build a bounded human preview while retaining authoritative counts.

    The full filename list remains in the event data/cycle trace. Only the
    rendered message is bounded so a large batch cannot produce an oversized
    status update.
    """

    normalized = [str(value).strip() for value in filenames if str(value).strip()]
    displayed: list[str] = []
    for filename in normalized[:ARTIFACT_PROGRESS_FILENAME_PREVIEW_MAX_ITEMS]:
        if len(filename) > ARTIFACT_PROGRESS_FILENAME_MAX_CHARS:
            filename = filename[: ARTIFACT_PROGRESS_FILENAME_MAX_CHARS - 1] + "…"
        displayed.append(filename)

    omitted_count = max(0, len(normalized) - len(displayed))
    preview = ", ".join(displayed)
    if omitted_count:
        preview = f"{preview}, … (+{omitted_count})" if preview else f"… (+{omitted_count})"

    return {
        "filename": normalized[0] if normalized else "",
        "file_count": len(normalized),
        "filenames_preview": preview,
        "filenames_preview_count": len(displayed),
        "filenames_omitted_count": omitted_count,
    }


def register_artifact_progress_messages() -> None:
    for locale_name, messages in _ARTIFACT_PROGRESS_MESSAGES.items():
        PROGRESS_MESSAGES.setdefault(locale_name, {}).update(messages)


register_artifact_progress_messages()
