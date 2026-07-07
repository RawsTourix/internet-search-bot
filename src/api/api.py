import os
import logging
from logging.handlers import RotatingFileHandler

# Импорт модулей
from .config import HTTP_PROXY, HTTPS_PROXY, AGENT_CONFIG_PATH
from ..mcp.mcp_client import MCPClient, load_config
from ..core.models import ClientType, AgentStatus, AgentResult
from ..core.errors import APIError

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
            logger.info("Загрузка конфигурации MCP-серверов и LLM")
            self.server_configs, self.llm_config = load_config(config_path)

            # Логирование конфигурации
            logger.debug(f"server_configs: {self.server_configs}")
            logger.debug(f"llm_config: {self.llm_config}")

            # Создание и запуск клиента
            logger.info("Инициализация MCP-клиента")
            self.mcp_client = MCPClient(self.llm_config)
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
            logger.debug(f"message: {message}")
            logger.debug(f"session_id: {session_id}")
            logger.debug(f"client_type: {client_type}")

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
