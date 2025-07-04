import os
import logging
from logging.handlers import RotatingFileHandler

# Импорт модулей
from .config import HTTP_PROXY, HTTPS_PROXY, MAIN_BOT_CONFIG_PATH
from ..bots.main_bot.mcp_client import MCPClient, load_config

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
    """API для работы с ботами"""
    def __init__(self, config_path):
        """Инициализация Api"""
        try:
            # Загрузка конфигурации
            logger.info("Загрузка конфигурации главного бота")
            self.server_config, self.llm_config = load_config(config_path)

            # Логирование конфигурации
            logger.debug(f"server_config: {self.server_config}")
            logger.debug(f"llm_config: {self.llm_config}")

            # Создание и запуск клиента
            logger.info("Инициализация клиента главного бота")
            self.main_bot_client = MCPClient(self.llm_config)
        except Exception as e:
            logger.critical(f"Ошибка инициализации Api: {e}")

    async def start(self):
        """Подключение к серверу главного бота"""
        try:
            logger.info("Подключение к серверу главного бота")
            await self.main_bot_client.connect_to_server(self.server_config)
        except Exception as e:
            logger.critical(f"Ошибка подключения к серверу: {e}")

    async def process_query(self, message: str) -> str:
        """Вызов главного бота"""
        try:
            if not self.main_bot_client.list_tools():
                logger.warning("list_tools главного бота пустой")

            logger.info("Вызов главного бота")
            logger.debug(f"message: {message}")

            response = await self.main_bot_client.process_query(message)
            logger.info("Ответ получен")
            return response
        except Exception as e:
            logger.error(f"Ошибка при вызове главного бота: {e}")
            return f"Ошибка при обработке запроса: {e}"
    
    async def stop(self):
        """Отключение от сервера главного бота"""
        try:
            await self.main_bot_client.cleanup()
        except Exception as e:
            logger.error(f"Ошибка при отключении от сервера: {e}")

API = Api(MAIN_BOT_CONFIG_PATH)

# Тестирование
async def main():
    try:
        await API.start()
        response = await API.process_query("")
        logger.info(f"response: {response}")
    finally:
        await API.stop()


if __name__ == '__main__':
    import asyncio
    asyncio.run(main())