"""Artifact-domain progress localization registered without transport coupling."""

from ..agent.progress_messages import PROGRESS_MESSAGES


_ARTIFACT_PROGRESS_MESSAGES = {
    "ru": {
        "artifact_candidate_list": "📦 Проверяю доступные результаты файловой обработки…",
        "artifact_create_from_content": "📦 Сохраняю результат обработки как файл…",
        "artifact_create_version_from_content": "📦 Создаю новую версию из результата обработки…",
        "artifact_set_delivery": "📤 Выбираю файл для отправки…",
        "artifact_ingested": "✅ Входной файл сохранён: {filename}",
        "artifact_delivery_selected": "📤 Файл выбран для отправки: {filename}",
        "artifact_delivery_cancelled": "⛔ Отправка файла отменена: {filename}",
        "artifact_delivery_started": "📤 Отправляю файл: {filename}",
        "artifact_delivery_done": "✅ Файл отправлен: {filename}",
        "artifact_delivery_failed": "⚠️ Не удалось отправить файл: {filename}",
        "artifact_delivery_unknown": "⚠️ Результат отправки файла неизвестен: {filename}",
    },
    "en": {
        "artifact_candidate_list": "📦 Checking available file-processing results…",
        "artifact_create_from_content": "📦 Saving the processing result as a file…",
        "artifact_create_version_from_content": "📦 Creating a new version from the processing result…",
        "artifact_set_delivery": "📤 Selecting a file for delivery…",
        "artifact_ingested": "✅ Input file stored: {filename}",
        "artifact_delivery_selected": "📤 File selected for delivery: {filename}",
        "artifact_delivery_cancelled": "⛔ File delivery cancelled: {filename}",
        "artifact_delivery_started": "📤 Sending file: {filename}",
        "artifact_delivery_done": "✅ File sent: {filename}",
        "artifact_delivery_failed": "⚠️ Failed to send file: {filename}",
        "artifact_delivery_unknown": "⚠️ File delivery result is unknown: {filename}",
    },
}


def register_artifact_progress_messages() -> None:
    for locale_name, messages in _ARTIFACT_PROGRESS_MESSAGES.items():
        PROGRESS_MESSAGES.setdefault(locale_name, {}).update(messages)


register_artifact_progress_messages()
