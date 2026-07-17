import logging
import os
from typing import Any, Iterable
from dotenv import load_dotenv

# Загрузка переменных из .env
load_dotenv()

# Прокси
HTTP_PROXY = os.getenv("HTTP_PROXY", "")
HTTPS_PROXY = os.getenv("HTTPS_PROXY", "")

# Путь к настройкам ботов
AGENT_CONFIG_PATH = os.getenv("AGENT_CONFIG_PATH", "")


def safe_llm_config_summary(config: Any) -> dict[str, Any]:
    """Return only non-sensitive LLM fields suitable for application logs."""
    return {
        "model": config.model,
        "api_url": config.api_url,
        "openai_compatible": config.is_openai_compatible,
        "context_window_tokens": config.context_window_tokens,
        "final_audit": config.final_audit,
    }


def safe_mcp_server_config_summary(configs: Iterable[Any]) -> list[dict[str, Any]]:
    """Return MCP connection metadata without headers, env, URLs or commands."""
    return [
        {
            "name": item.name,
            "alias": item.alias,
            "connect_type": item.connect_type.value,
            "enabled": item.enabled,
            "startup_required": item.startup_required,
        }
        for item in configs
    ]


def safe_memory_config_summary(config: Any) -> dict[str, Any]:
    """Return the explicit non-sensitive memory settings used by runtime."""
    return {
        "enable_result_compaction": config.enable_result_compaction,
        "inline_result_max_input_ratio": config.inline_result_max_input_ratio,
        "single_pass_summary_max_input_ratio": (
            config.single_pass_summary_max_input_ratio
        ),
        "result_summary_target_ratio": config.result_summary_target_ratio,
        "result_preview_max_chars": config.result_preview_max_chars,
    }
