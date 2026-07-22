import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Any, Dict

from .models import ClientType, MessageType, UnifiedMessage, UnifiedResponse
from .response_metadata import agent_result_metadata
from .session_ids import resolve_message_session_id
from ..api.api import API
from ..ingress import CommittedInputBatch


log_dir = "logging"
os.makedirs(log_dir, exist_ok=True)
formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("MessageProcessor")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    file_handler = RotatingFileHandler(
        filename=os.path.join(log_dir, "message_processor.log"),
        maxBytes=8 * 1024 * 1024,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)


class MessageProcessor:
    """Central processor for compatibility messages and committed batches."""

    def __init__(self):
        self.stats = {
            "total_messages": 0,
            "messages_by_client": {
                client.value: 0 for client in ClientType
            },
            "errors": 0,
            "start_time": datetime.now(),
        }
        self.active_sessions = {}

    async def process_message(
        self,
        message: UnifiedMessage,
        progress_callback=None,
    ) -> UnifiedResponse:
        try:
            logger.info(
                "Обработка сообщения от %s, content_chars=%s",
                message.client_type,
                len(message.content or ""),
            )
            self._record_request(message.client_type)
            return await self._generate_response(
                message,
                progress_callback=progress_callback,
            )
        except Exception as error:
            logger.error("Ошибка обработки сообщения: %s", error)
            self.stats["errors"] += 1
            return UnifiedResponse(
                message_id=message.id,
                client_type=message.client_type,
                content=f"Произошла ошибка при обработке сообщения: {error}",
                response_type=MessageType.TEXT,
            )

    async def process_committed_batch(
        self,
        batch: CommittedInputBatch,
        *,
        progress_callback=None,
        progress_locale: str = "ru",
    ) -> UnifiedResponse:
        try:
            self._record_request(batch.client_type)
            result = await API.call_agent_batch(
                batch.input_batch_id,
                session_id=batch.session_id,
                progress_callback=progress_callback,
                progress_locale=progress_locale,
            )
            return UnifiedResponse(
                message_id=batch.input_batch_id,
                client_type=batch.client_type,
                content=result.content,
                response_type=MessageType.TEXT,
                metadata=self._agent_result_metadata(result),
            )
        except Exception as error:
            logger.error(
                "Ошибка обработки committed batch %s: %r",
                batch.input_batch_id,
                error,
            )
            self.stats["errors"] += 1
            return UnifiedResponse(
                message_id=batch.input_batch_id,
                client_type=batch.client_type,
                content=f"Произошла ошибка при обработке batch: {error}",
                response_type=MessageType.TEXT,
                metadata={"input_batch_id": batch.input_batch_id},
            )

    def _record_request(self, client_type: ClientType) -> None:
        self.stats["total_messages"] += 1
        self.stats["messages_by_client"][client_type.value] += 1

    def _build_session_id(self, message: UnifiedMessage) -> str:
        return resolve_message_session_id(
            client_type=message.client_type,
            metadata=message.metadata,
            user_id=message.user_id,
        )

    async def _generate_response(
        self,
        message: UnifiedMessage,
        progress_callback=None,
    ) -> UnifiedResponse:
        response_content = ""
        response_metadata: dict[str, Any] = {}

        if message.message_type == MessageType.COMMAND:
            response_content = await self._handle_command(message)
        elif message.message_type == MessageType.TEXT:
            try:
                metadata = message.metadata or {}
                result = await API.call_agent(
                    message.content,
                    session_id=self._build_session_id(message),
                    client_type=message.client_type,
                    progress_callback=progress_callback,
                    progress_locale=metadata.get("progress_locale", "ru"),
                )
                response_content = result.content
                response_metadata = self._agent_result_metadata(result)
            except Exception as error:
                response_content = f"Сообщение не обработано: {error}"

        return UnifiedResponse(
            message_id=message.id,
            client_type=message.client_type,
            content=response_content,
            response_type=MessageType.TEXT,
            metadata=response_metadata,
        )

    @staticmethod
    def _agent_result_metadata(agent_result) -> dict[str, Any]:
        return agent_result_metadata(agent_result)

    async def _handle_command(self, message: UnifiedMessage) -> str:
        command = message.content.strip()
        if command == "/start":
            return (
                f"Привет, {message.user_name or message.user_id}! "
                "Я интеллектуальный помощник с доступом к инструментам."
            )
        if command == "/status":
            return await self._get_status_text()
        if command == "/help":
            return self._get_help_text()
        if command == "/reset":
            try:
                await API.reset(self._build_session_id(message))
                return "✅ Память успешно очищена."
            except Exception as error:
                return f"⚠️ Ошибка очистки памяти: {error}."
        return f"⚠️ Неизвестная команда: {command}"

    @staticmethod
    def _get_help_text() -> str:
        return """
Доступные команды:
/start - приветствие
/status - статус системы
/reset - очистка памяти
/help - справка

Вы можете отправлять текст и файлы для обработки.
        """.strip()

    async def _get_status_text(self) -> str:
        uptime = datetime.now() - self.stats["start_time"]
        return f"""
Статус Gateway:
• Время работы: {uptime}
• Всего сообщений: {self.stats['total_messages']}
• Ошибок: {self.stats['errors']}
• Telegram: {self.stats['messages_by_client']['telegram']}
        """.strip()

    async def get_stats(self) -> Dict[str, Any]:
        uptime = datetime.now() - self.stats["start_time"]
        return {
            **self.stats,
            "uptime_seconds": uptime.total_seconds(),
            "active_sessions": len(self.active_sessions),
        }
