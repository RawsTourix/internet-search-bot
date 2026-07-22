from pathlib import Path

path = Path("src/agent/progress_messages.py")
text = path.read_text(encoding="utf-8")

replacements = {
    '        "agent_plan_cancel": "🗺️ Отменяю план работы…",\n': (
        '        "agent_plan_cancel": "🗺️ Отменяю план работы…",\n'
        '        "artifact_list": "📁 Проверяю доступные файлы…",\n'
        '        "artifact_get": "📄 Получаю сведения о файле…",\n'
        '        "artifact_read_text": "📖 Читаю содержимое файла…",\n'
        '        "artifact_search_text": "🔎 Ищу данные в файле…",\n'
        '        "artifact_create_text": "📝 Создаю файл…",\n'
        '        "artifact_replace_text": "📝 Создаю новую версию файла…",\n'
        '        "artifact_patch_text": "📝 Вношу точные изменения в файл…",\n'
    ),
    '        "plan_waiting_user_blocked": "🗺️ Перед вопросом пользователю нужно приостановить активный этап.",\n': (
        '        "plan_waiting_user_blocked": "🗺️ Перед вопросом пользователю нужно приостановить активный этап.",\n'
        '        "artifact_created": "✅ Файл создан: {filename}",\n'
        '        "artifact_version_created": "✅ Создана новая версия файла: {filename}",\n'
        '        "artifact_version_conflict": "⚠️ Файл уже изменился; перечитываю актуальную версию.",\n'
        '        "artifact_validation_failed": "⚠️ Операция с файлом не прошла проверку.",\n'
    ),
    '        "agent_plan_cancel": "🗺️ Cancelling the work plan…",\n': (
        '        "agent_plan_cancel": "🗺️ Cancelling the work plan…",\n'
        '        "artifact_list": "📁 Checking available files…",\n'
        '        "artifact_get": "📄 Reading file metadata…",\n'
        '        "artifact_read_text": "📖 Reading file content…",\n'
        '        "artifact_search_text": "🔎 Searching the file…",\n'
        '        "artifact_create_text": "📝 Creating a file…",\n'
        '        "artifact_replace_text": "📝 Creating a new file version…",\n'
        '        "artifact_patch_text": "📝 Applying exact file changes…",\n'
    ),
    '        "plan_waiting_user_blocked": "🗺️ Pause the active stage before asking the user.",\n': (
        '        "plan_waiting_user_blocked": "🗺️ Pause the active stage before asking the user.",\n'
        '        "artifact_created": "✅ File created: {filename}",\n'
        '        "artifact_version_created": "✅ New file version created: {filename}",\n'
        '        "artifact_version_conflict": "⚠️ The file changed; reading the current version.",\n'
        '        "artifact_validation_failed": "⚠️ The file operation failed validation.",\n'
    ),
}

for anchor, replacement in replacements.items():
    if text.count(anchor) != 1:
        raise RuntimeError(f"progress message anchor changed: {anchor!r}")
    text = text.replace(anchor, replacement, 1)

path.write_text(text, encoding="utf-8")
