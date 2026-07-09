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
        "tool_start": "🔧 Запускаю инструмент {tool_name}…",
        "tool_done": "✅ Инструмент {tool_name} завершил работу.",
        "tool_error": "⚠️ Инструмент {tool_name} завершился с ошибкой.",
        "tool_timeout": "⚠️ Инструмент {tool_name} завершился по таймауту.",
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
        "infrastructure_interruption": (
            "⚠️ Инфраструктурная ошибка. Состояние задачи сохранено."
        ),
        "waiting_user": "❓ Нужны дополнительные данные от пользователя.",
        "cycle_done": "✅ Задача завершена.",
        "cycle_error": "⚠️ Задача завершилась с ошибкой.",
        "context_warning": "⚠️ Контекст задачи стал большим.",
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
        "tool_start": "🔧 Running tool {tool_name}…",
        "tool_done": "✅ Tool {tool_name} finished.",
        "tool_error": "⚠️ Tool {tool_name} failed.",
        "tool_timeout": "⚠️ Tool {tool_name} timed out.",
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
        "infrastructure_interruption": (
            "⚠️ Infrastructure error. Task state has been saved."
        ),
        "waiting_user": "❓ More information is needed from the user.",
        "cycle_done": "✅ Task completed.",
        "cycle_error": "⚠️ Task failed.",
        "context_warning": "⚠️ Task context is getting large.",
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
