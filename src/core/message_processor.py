import os
import logging
from typing import Dict, Any
from datetime import datetime
from logging.handlers import RotatingFileHandler

from .models import UnifiedMessage, UnifiedResponse, ClientType, MessageType
from ..api.api import API

# Проверяем и создаем папку для логов
log_dir = "logging"
if not os.path.exists(log_dir):
    os.makedirs(log_dir, exist_ok=True)

# Настройка логирования
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

logger = logging.getLogger("MessageProcessor")
logger.setLevel(logging.DEBUG)

file_handler = RotatingFileHandler(
    filename=os.path.join(log_dir, "message_processor.log"),
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

class MessageProcessor:
    """Центральный процессор сообщений"""
    
    def __init__(self):
        self.stats = {
            "total_messages": 0,
            "messages_by_client": {client.value: 0 for client in ClientType},
            "errors": 0,
            "start_time": datetime.now()
        }
        self.active_sessions = {}
        
    async def process_message(
        self,
        message: UnifiedMessage,
        progress_callback=None,
    ) -> UnifiedResponse:
        """Обработка унифицированного сообщения"""
        try:
            logger.info(f"Обработка сообщения от {message.client_type}: {message.content}")
            
            # Обновление статистики
            self.stats["total_messages"] += 1
            self.stats["messages_by_client"][message.client_type.value] += 1
            
            # Получение ответа
            response = await self._generate_response(
                message,
                progress_callback=progress_callback,
            )
            
            return response
            
        except Exception as e:
            logger.error(f"Ошибка обработки сообщения: {e}")
            self.stats["errors"] += 1
            
            return UnifiedResponse(
                message_id=message.id,
                client_type=message.client_type,
                content=f"Произошла ошибка при обработке сообщения: {str(e)}",
                response_type=MessageType.TEXT
            )
        
    def _build_session_id(self, message: UnifiedMessage) -> str:
        """Строит идентификатор сессии"""
        metadata = message.metadata or {}

        session_id = metadata.get("session_id")
        if session_id is not None:
            return f"{message.client_type.value}:session:{session_id}"

        chat_id = metadata.get("chat_id")
        if chat_id is not None:
            return f"{message.client_type.value}:chat:{chat_id}"

        return f"{message.client_type.value}:user:{message.user_id}"
    
    async def _generate_response(
        self,
        message: UnifiedMessage,
        progress_callback=None,
    ) -> UnifiedResponse:
        """Обработка запроса"""
        response_content = ""
        response_metadata = {}

        if message.message_type == MessageType.COMMAND:
            response_content = await self._handle_command(message)
        elif message.message_type == MessageType.TEXT:
            try:
                session_id = self._build_session_id(message)
                client_type = message.client_type
                metadata = message.metadata or {}
                progress_locale = metadata.get("progress_locale", "ru")
                agent_result  = await API.call_agent(
                    message.content,
                    session_id=session_id,
                    client_type=client_type,
                    progress_callback=progress_callback,
                    progress_locale=progress_locale,
                )
                response_content = agent_result.content
                response_metadata = {
                    "agent_status": agent_result.status,
                    "session_id": agent_result.session_id,
                    "iterations": agent_result.iterations,
                    "tools_used": agent_result.tools_used,
                    "error": agent_result.error,
                    "error_kind": agent_result.error_kind,
                    "can_resume": agent_result.can_resume,
                    "progress_events": agent_result.progress_events
                }

            except Exception as e:
                response_content = f"Сообщение не обработано: {e}"
        
        response = UnifiedResponse(
            message_id=message.id,
            client_type=message.client_type,
            content=response_content,
            response_type=MessageType.TEXT,
            metadata=response_metadata
        )
        
        return response
        
    async def _handle_command(self, message: UnifiedMessage) -> str:
        """Обработка команд"""
        command = message.content.strip()
        
        # Запуск
        if command == "/start":
            return f"Привет, {message.user_name or message.user_id}! Я интеллектуальный помощник с доступом к интернет-поиску. Задавай любые вопросы — буду рад ответить! 😊"
        # Статус
        elif command == "/status":
            return await self._get_status_text()
        # Справка
        elif command == "/help":
            return self._get_help_text()
        # Очистка памяти сессии
        elif command == "/reset":
            try:
                await API.reset(self._build_session_id(message))
                return "✅ Память успешно очищена."
            except Exception as e:
                return f"⚠️ Ошибка очистки памяти: {e}."
        else:
            return f"⚠️ Неизвестная команда: {command}"
    
    def _get_help_text(self) -> str:
        """Справочная информация"""
        return """
Доступные команды:
/start - приветствие
/status - статус системы
/reset - очиска памяти
/help - справка

Вы можете отправлять любые текстовые сообщения для обработки.
        """.strip()
    
    async def _get_status_text(self) -> str:
        """Информация о статусе"""
        uptime = datetime.now() - self.stats["start_time"]
        return f"""
Статус Gateway:
• Время работы: {uptime}
• Всего сообщений: {self.stats['total_messages']}
• Ошибок: {self.stats['errors']}
• Сообщений по типам:
  - Telegram: {self.stats['messages_by_client']['telegram']}
        """.strip()
    
    async def get_stats(self) -> Dict[str, Any]:
        """Получение статистики"""
        uptime = datetime.now() - self.stats["start_time"]
        return {
            **self.stats,
            "uptime_seconds": uptime.total_seconds(),
            "active_sessions": len(self.active_sessions)
        }
