import os
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Импорт модулей
from .config import (
    AGENT_CONFIG_PATH,
    HTTP_PROXY,
    HTTPS_PROXY,
    safe_artifact_config_summary,
    safe_llm_config_summary,
    safe_memory_config_summary,
    safe_mcp_server_config_summary,
    safe_planning_config_summary,
    safe_runtime_config_summary,
)
from ..artifacts import (
    apply_local_workspace_server_policy,
    create_artifact_services,
    load_artifact_config,
)
from ..mcp.mcp_client import load_config
from ..mcp.planning_runtime import FinalizingPlanningMCPClient
from ..core.models import ClientType, AgentStatus, AgentResult
from ..core.errors import APIError
from ..planning import (
    create_planning_services,
    load_planning_config,
)
from ..planning.runtime_context import PlanningAwareContentStore
from ..storage import StorageServices, create_storage_services

# Настройка прокси
os.environ['http_proxy'] = HTTP_PROXY
os.environ['https_proxy'] = HTTPS_PROXY

# Проверяем и создаем папку для логов
log_dir = "logging"
if not os.path.exists(log_dir):
    os.makedirs(log_dir, exist_ok=True)

# Настройка логирования
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

logger = logging.getLogger("API")
logger.setLevel(logging.DEBUG)

file_handler = RotatingFileHandler(
    filename=os.path.join(log_dir, "api.log"),
    maxBytes=8*1024*1024,  # 8 MB
    encoding='utf-8'
)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)

logger.addHandler(file_handler)
logger.addHandler(console_handler)


class Api:
    """API для работы с агентом"""

    def __init__(self, config_path):
        """Инициализация Api"""
        try:
            # Загрузка конфигурации
            logger.info(
                "Загрузка конфигурации MCP-серверов, LLM, storage, "
                "memory, runtime, planning и artifacts"
            )
            (
                self.server_configs,
                self.llm_config,
                self.storage_config,
                self.memory_config,
                self.runtime_config,
            ) = load_config(config_path)
            self.planning_config = load_planning_config(config_path)
            self.artifact_config = load_artifact_config(config_path)
            apply_local_workspace_server_policy(
                self.server_configs,
                self.artifact_config,
            )

            # Логируем только безопасную сводку: конфигурация также содержит
            # api_key, Authorization headers и env-переменные MCP-серверов.
            llm_log_summary = safe_llm_config_summary(self.llm_config)
            logger.debug(
                "LLM config: model=%s api_url=%s openai_compatible=%s "
                "context_window_tokens=%s tokenizer_encoding=%s "
                "final_audit=%s",
                llm_log_summary["model"],
                llm_log_summary["api_url"],
                llm_log_summary["openai_compatible"],
                llm_log_summary["context_window_tokens"],
                llm_log_summary["tokenizer_encoding"],
                llm_log_summary["final_audit"],
            )
            logger.debug(
                "MCP servers configured: %s",
                safe_mcp_server_config_summary(self.server_configs),
            )

            storage_root = Path(self.storage_config.root_dir).expanduser()
            if not storage_root.is_absolute():
                storage_root = Path.cwd() / storage_root
            logger.info(
                "Storage: backend=%s root=%s atomic_writes=%s "
                "verify_content_hash=%s max_in_memory_content_bytes=%s",
                self.storage_config.backend,
                storage_root.resolve(strict=False),
                self.storage_config.atomic_writes,
                self.storage_config.verify_content_hash,
                self.storage_config.max_in_memory_content_bytes,
            )
            logger.info(
                "Memory result compaction: %s",
                safe_memory_config_summary(self.memory_config),
            )
            logger.info(
                "Runtime lifecycle: %s",
                safe_runtime_config_summary(self.runtime_config),
            )
            logger.info(
                "DAG planning: %s",
                safe_planning_config_summary(self.planning_config),
            )
            logger.info(
                "Artifacts: %s",
                safe_artifact_config_summary(self.artifact_config),
            )

            base_storage_services = create_storage_services(self.storage_config)
            self.storage_services = StorageServices(
                config=base_storage_services.config,
                content_store=PlanningAwareContentStore(
                    base_storage_services.content_store
                ),
                artifact_store=base_storage_services.artifact_store,
            )
            self.artifact_services = create_artifact_services(
                storage_config=self.storage_config,
                artifact_config=self.artifact_config,
                content_store=self.storage_services.content_store,
            )
            self.planning_services = create_planning_services(
                storage_config=self.storage_config,
                planning_config=self.planning_config,
            )

            # Создание и запуск клиента
            logger.info("Инициализация artifact/planning-aware MCP-клиента")
            self.mcp_client = FinalizingPlanningMCPClient(
                self.llm_config,
                storage_services=self.storage_services,
                artifact_services=self.artifact_services,
                memory_config=self.memory_config,
                runtime_config=self.runtime_config,
                planning_services=self.planning_services,
            )
        except Exception as e:
            raise APIError(f"Ошибка инициализации Api: {repr(e)}") from e

    async def start(self):
        """Подключение к MCP-серверам"""
        try:
            logger.info("Подключение к MCP-серверам")
            await self.mcp_client.connect_to_servers(self.server_configs)
        except Exception as e:
            raise APIError(f"Ошибка подключения к MCP-серверам: {repr(e)}") from e

    async def call_agent(
        self,
        message: str,
        session_id: str = "default",
        client_type: ClientType | None = None,
        progress_callback=None,
        progress_locale: str = "ru",
    ) -> AgentResult:
        """Обращение к MCP-клиенту"""
        try:
            if not await self.mcp_client.list_tools():
                logger.warning("Нет зарегистрированных инструментов")

            logger.info("Вызов ИИ-агента")
            logger.debug("message_chars: %s", len(message))
            logger.debug("session_id: %s", session_id)
            logger.debug("client_type: %s", client_type)

            agent_result = await self.mcp_client.process_query(
                message,
                session_id=session_id,
                client_type=client_type,
                progress_callback=progress_callback,
                progress_locale=progress_locale,
            )
            logger.info("Ответ получен")
            return agent_result
        except Exception as e:
            logger.error(f"Ошибка при обращении к MCP-клиенту: {e}")

            state = None
            try:
                state = self.mcp_client.get_session_state(session_id)
            except Exception:
                state = None

            result_text = f"Ошибка при обработке запроса: {e}"

            return AgentResult(
                content=result_text,
                status=AgentStatus.ERROR,
                session_id=session_id,
                iterations=state.iterations if state else 0,
                tools_used=state.tools_used if state else [],
                error=str(e),
                error_kind="critical_error",
                can_resume=False,
                progress_events=state.progress_events if state else []
            )

    async def reset(self, session_id: str):
        """Очистка памяти сессии"""
        self.mcp_client.clear_session(session_id)

    async def stop(self):
        """Отключение от сервера главного бота"""
        try:
            await self.mcp_client.cleanup()
        except Exception as e:
            logger.error(f"Ошибка при отключении от сервера: {e}")


API = Api(AGENT_CONFIG_PATH)

"""
# Тестирование
async def main():
    try:
        await API.start()
        response = await API.call_agent("")
        logger.info(f"response: {response}")
    finally:
        await API.stop()


if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
"""
