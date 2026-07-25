from typing import Any


PROGRESS_MESSAGES: dict[str, dict[str, str]] = {
    "ru": {
        "cycle_started": "🧭 Начинаю обработку задачи…",
        "cycle_resumed": "▶️ Продолжаю задачу с учётом ответа…",
        "iteration_started": "Итерация {iteration}/{max_iterations}",
        "mcp_list_servers": "🔎 Проверяю доступные MCP-серверы…",
        "mcp_list_tools": "🧰 Получаю список доступных инструментов…",
        "mcp_get_tool_schema": "📋 Проверяю схему {tool_name}…",
        "mcp_call_tool": "🔧 Запускаю {tool_name}…",
        "mcp_get_runtime_context": "🕒 Получаю runtime-контекст агента…",
        "agent_plan_create": "🗺️ Создаю план работы…",
        "agent_plan_get": "🗺️ Проверяю план работы…",
        "agent_plan_add_nodes": "🗺️ Добавляю этапы в план…",
        "agent_plan_update_node": "🗺️ Уточняю этап плана…",
        "agent_plan_transition_node": "🗺️ Обновляю состояние этапа…",
        "agent_plan_remove_node": "🗺️ Удаляю неактуальный этап…",
        "agent_plan_cancel": "🗺️ Отменяю план работы…",
        "artifact_list": "📁 Проверяю доступные файлы…",
        "artifact_read_text": "📖 Читаю содержимое файла…",
        "artifact_search_text": "🔎 Ищу данные в файле…",
        "artifact_create_text": "📝 Создаю файл…",
        "artifact_replace_text": "📝 Создаю новую версию файла…",
        "artifact_patch_text": "📝 Вношу точные изменения в файл…",
        "tool_start": "🔧 Запускаю инструмент {tool_name}…",
        "tool_done": "✅ Инструмент {tool_name} завершил работу.",
        "tool_rejected": "⚠️ Инструмент {tool_name} отклонил операцию.",
        "tool_failed": "❌ Инструмент {tool_name} завершился с ошибкой.",
        "tool_error": "⚠️ Инструмент {tool_name} завершился с ошибкой.",
        "tool_timeout": "⚠️ Инструмент {tool_name} завершился по таймауту.",
        "tool_result_unavailable": (
            "⚠️ Результат инструмента «{tool_name}» недоступен "
            "для дальнейшей обработки."
        ),
        "llm_http_retry": (
            "⚠️ LLM HTTP {status_code}. Повтор через {delay:.0f} сек. "
            "Попытка {attempt}/{max_attempts}…"
        ),
        "llm_http_exhausted": (
            "⚠️ LLM HTTP {status_code}. Повторы исчерпаны. "
            "Попытка {attempt}/{max_attempts}."
        ),
        "llm_http_non_retryable": (
            "⚠️ LLM HTTP {status_code}. Повтор не выполняется."
        ),
        "llm_transport_retry": (
            "⚠️ LLM transport error. Повтор через {delay:.0f} сек. "
            "Попытка {attempt}/{max_attempts}…"
        ),
        "llm_transport_exhausted": (
            "⚠️ LLM transport error. Повторы исчерпаны. "
            "Попытка {attempt}/{max_attempts}."
        ),
        "llm_timeout_retry": (
            "⚠️ LLM timeout. Повтор через {delay:.0f} сек. "
            "Попытка {attempt}/{max_attempts}…"
        ),
        "llm_timeout_exhausted": (
            "⚠️ LLM timeout. Повторы исчерпаны. "
            "Попытка {attempt}/{max_attempts}."
        ),
        "llm_response_error": (
            "⚠️ LLM вернула некорректный ответ. Повтор не выполняется."
        ),
        "infrastructure_interruption": (
            "⚠️ Инфраструктурная ошибка. Состояние задачи сохранено."
        ),
        "context_limit_interruption": (
            "⚠️ Рабочий контекст достиг предельного размера. "
            "Состояние задачи сохранено."
        ),
        "final_processing_started": "✍️ Готовлю финальный ответ…",
        "final_processing_format_only": "🪄 Оформляю финальный ответ…",
        "final_processing_grounded": (
            "🔎 Проверяю финальный ответ по собранным данным…"
        ),
        "final_processing_strict_grounded": (
            "🧩 Сверяю детали перед финальным ответом…"
        ),
        "waiting_user": "❓ Нужны дополнительные данные от пользователя.",
        "result_ready": "📦 Результат подготовлен. Выполняется доставка…",
        "cycle_done": "✅ Задача завершена.",
        "cycle_error": "⚠️ Задача завершилась с ошибкой.",
        "context_warning": "⚠️ Контекст задачи стал большим.",
        "cycle_compaction_started": "🧠 Освобождаю рабочий контекст…",
        "cycle_compaction_done": "✅ Рабочий контекст обновлён.",
        "cycle_compaction_failed": (
            "⚠️ Не удалось безопасно сжать рабочий контекст."
        ),
        "result_persist_started": "💾 Сохраняю большой результат…",
        "result_persist_done": "✅ Большой результат сохранён.",
        "result_persist_failed": "⚠️ Не удалось сохранить большой результат.",
        "result_compaction_started": "🧩 Сжимаю большой результат…",
        "result_compaction_done": "✅ Большой результат сжат.",
        "result_compaction_failed": (
            "⚠️ Не удалось создать краткое описание результата."
        ),
        "oversized_result_stored": (
            "📦 Результат сохранён для последующей обработки."
        ),
        "plan_created": "🗺️ Создан план работы.",
        "plan_revised": "🗺️ План работы обновлён.",
        "plan_node_started": "▶️ Выполняю этап: {node_title}",
        "plan_node_completed": "✅ Этап выполнен: {node_title}",
        "plan_node_blocked": "⏸️ Этап приостановлен: {node_title}",
        "plan_node_failed": "⚠️ Этап завершился ошибкой: {node_title}",
        "plan_node_skipped": "⏭️ Этап пропущен: {node_title}",
        "plan_completed": "✅ План работы выполнен.",
        "plan_cancelled": "⛔ План работы отменён.",
        "plan_revision_conflict": "⚠️ План уже изменился; перечитываю актуальную ревизию.",
        "plan_validation_failed": "⚠️ Изменение плана не прошло проверку.",
        "plan_finalization_blocked": "🗺️ Сначала нужно согласовать незавершённый план.",
        "plan_waiting_user_blocked": "🗺️ Перед вопросом пользователю нужно приостановить активный этап.",
        "artifact_created": "✅ Файл создан: {filename}",
        "artifact_version_created": "✅ Создана новая версия файла: {filename}",
        "artifact_version_conflict": "⚠️ Файл уже изменился; перечитываю актуальную версию.",
        "artifact_validation_failed": "⚠️ Операция с файлом не прошла проверку.",
    },
    "en": {
        "cycle_started": "🧭 Starting task processing…",
        "cycle_resumed": "▶️ Continuing the task with the new reply…",
        "iteration_started": "Iteration {iteration}/{max_iterations}",
        "mcp_list_servers": "🔎 Checking available MCP servers…",
        "mcp_list_tools": "🧰 Getting available tools…",
        "mcp_get_tool_schema": "📋 Checking schema for {tool_name}…",
        "mcp_call_tool": "🔧 Running {tool_name}…",
        "mcp_get_runtime_context": "🕒 Getting agent runtime context…",
        "agent_plan_create": "🗺️ Creating a work plan…",
        "agent_plan_get": "🗺️ Reading the work plan…",
        "agent_plan_add_nodes": "🗺️ Adding plan stages…",
        "agent_plan_update_node": "🗺️ Updating a plan stage…",
        "agent_plan_transition_node": "🗺️ Updating stage state…",
        "agent_plan_remove_node": "🗺️ Removing an obsolete stage…",
        "agent_plan_cancel": "🗺️ Cancelling the work plan…",
        "artifact_list": "📁 Checking available files…",
        "artifact_read_text": "📖 Reading file content…",
        "artifact_search_text": "🔎 Searching the file…",
        "artifact_create_text": "📝 Creating a file…",
        "artifact_replace_text": "📝 Creating a new file version…",
        "artifact_patch_text": "📝 Applying exact file changes…",
        "tool_start": "🔧 Running tool {tool_name}…",
        "tool_done": "✅ Tool {tool_name} finished.",
        "tool_rejected": "⚠️ Tool {tool_name} rejected the operation.",
        "tool_failed": "❌ Tool {tool_name} failed.",
        "tool_error": "⚠️ Tool {tool_name} failed.",
        "tool_timeout": "⚠️ Tool {tool_name} timed out.",
        "tool_result_unavailable": (
            "⚠️ The result from tool {tool_name} is unavailable "
            "for further processing."
        ),
        "llm_http_retry": (
            "⚠️ LLM HTTP {status_code}. Retrying in {delay:.0f}s. "
            "Attempt {attempt}/{max_attempts}…"
        ),
        "llm_http_exhausted": (
            "⚠️ LLM HTTP {status_code}. Retries exhausted. "
            "Attempt {attempt}/{max_attempts}."
        ),
        "llm_http_non_retryable": (
            "⚠️ LLM HTTP {status_code}. Retry is not allowed."
        ),
        "llm_transport_retry": (
            "⚠️ LLM transport error. Retrying in {delay:.0f}s. "
            "Attempt {attempt}/{max_attempts}…"
        ),
        "llm_transport_exhausted": (
            "⚠️ LLM transport error. Retries exhausted. "
            "Attempt {attempt}/{max_attempts}."
        ),
        "llm_timeout_retry": (
            "⚠️ LLM timeout. Retrying in {delay:.0f}s. "
            "Attempt {attempt}/{max_attempts}…"
        ),
        "llm_timeout_exhausted": (
            "⚠️ LLM timeout. Retries exhausted. "
            "Attempt {attempt}/{max_attempts}."
        ),
        "llm_response_error": (
            "⚠️ LLM returned an invalid response. Retry is not allowed."
        ),
        "infrastructure_interruption": (
            "⚠️ Infrastructure error. Task state has been saved."
        ),
        "context_limit_interruption": (
            "⚠️ The working context reached its limit. "
            "Task state has been saved."
        ),
        "final_processing_started": "✍️ Preparing the final answer…",
        "final_processing_format_only": "🪄 Polishing the final answer…",
        "final_processing_grounded": (
            "🔎 Checking the final answer against the collected data…"
        ),
        "final_processing_strict_grounded": (
            "🧩 Checking the details before the final answer…"
        ),
        "waiting_user": "❓ More information is needed from the user.",
        "result_ready": "📦 The result is ready. Delivering it now…",
        "cycle_done": "✅ Task completed.",
        "cycle_error": "⚠️ Task failed.",
        "context_warning": "⚠️ Task context is getting large.",
        "cycle_compaction_started": "🧠 Freeing working context…",
        "cycle_compaction_done": "✅ Working context updated.",
        "cycle_compaction_failed": (
            "⚠️ Failed to compact the working context safely."
        ),
        "result_persist_started": "💾 Saving the large result…",
        "result_persist_done": "✅ Large result saved.",
        "result_persist_failed": "⚠️ Failed to save the large result.",
        "result_compaction_started": "🧩 Compacting the large result…",
        "result_compaction_done": "✅ Large result compacted.",
        "result_compaction_failed": (
            "⚠️ Failed to create a summary of the result."
        ),
        "oversized_result_stored": (
            "📦 Result saved for later processing."
        ),
        "plan_created": "🗺️ Work plan created.",
        "plan_revised": "🗺️ Work plan updated.",
        "plan_node_started": "▶️ Working on stage: {node_title}",
        "plan_node_completed": "✅ Stage completed: {node_title}",
        "plan_node_blocked": "⏸️ Stage paused: {node_title}",
        "plan_node_failed": "⚠️ Stage failed: {node_title}",
        "plan_node_skipped": "⏭️ Stage skipped: {node_title}",
        "plan_completed": "✅ Work plan completed.",
        "plan_cancelled": "⛔ Work plan cancelled.",
        "plan_revision_conflict": "⚠️ The plan changed; reading the current revision.",
        "plan_validation_failed": "⚠️ The plan change failed validation.",
        "plan_finalization_blocked": "🗺️ The unfinished plan must be reconciled first.",
        "plan_waiting_user_blocked": "🗺️ Pause the active stage before asking the user.",
        "artifact_created": "✅ File created: {filename}",
        "artifact_version_created": "✅ New file version created: {filename}",
        "artifact_version_conflict": "⚠️ The file changed; reading the current version.",
        "artifact_validation_failed": "⚠️ The file operation failed validation.",
    },
}


PROGRESS_MESSAGE_DEFAULT_KWARGS: dict[str, dict[str, dict[str, str]]] = {
    "mcp_get_tool_schema": {
        "tool_name": {
            "ru": "инструмента",
            "en": "tool",
        },
    },
    "mcp_call_tool": {
        "tool_name": {
            "ru": "инструмент",
            "en": "tool",
        },
    },
    "tool_start": {
        "tool_name": {
            "ru": "инструмент",
            "en": "tool",
        },
    },
    "tool_done": {
        "tool_name": {
            "ru": "инструмент",
            "en": "tool",
        },
    },
    "tool_rejected": {
        "tool_name": {
            "ru": "инструмент",
            "en": "tool",
        },
    },
    "tool_failed": {
        "tool_name": {
            "ru": "инструмент",
            "en": "tool",
        },
    },
    "tool_error": {
        "tool_name": {
            "ru": "инструмент",
            "en": "tool",
        },
    },
    "tool_timeout": {
        "tool_name": {
            "ru": "инструмент",
            "en": "tool",
        },
    },
    "tool_result_unavailable": {
        "tool_name": {
            "ru": "неизвестный",
            "en": "unknown",
        },
    },
    "plan_node_started": {
        "node_title": {"ru": "этап", "en": "stage"},
    },
    "plan_node_completed": {
        "node_title": {"ru": "этап", "en": "stage"},
    },
    "plan_node_blocked": {
        "node_title": {"ru": "этап", "en": "stage"},
    },
    "plan_node_failed": {
        "node_title": {"ru": "этап", "en": "stage"},
    },
    "plan_node_skipped": {
        "node_title": {"ru": "этап", "en": "stage"},
    },
}


DEFAULT_PROGRESS_LOCALE = "ru"


def normalize_progress_locale(value: str | None) -> str:
    if not value:
        return DEFAULT_PROGRESS_LOCALE

    value = value.lower().strip().replace("_", "-")

    if value in PROGRESS_MESSAGES:
        return value

    language = value.split("-", 1)[0]

    if language in PROGRESS_MESSAGES:
        return language

    return DEFAULT_PROGRESS_LOCALE


def _localized_default(
    key: str,
    arg_name: str,
    locale_name: str,
) -> str | None:
    key_defaults = PROGRESS_MESSAGE_DEFAULT_KWARGS.get(key) or {}
    arg_defaults = key_defaults.get(arg_name) or {}

    return arg_defaults.get(locale_name) or arg_defaults.get("ru")


def _merge_default_kwargs(
    key: str,
    locale_name: str,
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    key_defaults = PROGRESS_MESSAGE_DEFAULT_KWARGS.get(key) or {}
    result = dict(kwargs)

    for arg_name in key_defaults:
        if result.get(arg_name) not in (None, ""):
            continue

        default_value = _localized_default(key, arg_name, locale_name)
        if default_value is not None:
            result[arg_name] = default_value

    return result


def progress_text(
    key: str,
    *,
    locale_name: str | None = None,
    **kwargs: Any,
) -> str:
    locale_name = normalize_progress_locale(locale_name)
    messages = PROGRESS_MESSAGES.get(locale_name) or PROGRESS_MESSAGES["ru"]
    template = messages.get(key) or PROGRESS_MESSAGES["ru"].get(key) or key
    kwargs = _merge_default_kwargs(key, locale_name, kwargs)

    try:
        return template.format(**kwargs)
    except Exception:
        return template
