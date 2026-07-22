import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import TYPE_CHECKING, Any, Dict

from .telegram_ingress import build_telegram_input_envelope
from ..core.models import (
    AdapterStatus,
    MessageType,
    UnifiedMessage,
    UnifiedResponse,
)
from ..ingress import ClientInputEnvelope, InputAdmissionMode

if TYPE_CHECKING:
    from ..core.message_processor import MessageProcessor


log_dir = "logging"
os.makedirs(log_dir, exist_ok=True)
formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("TelegramAdapter")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    file_handler = RotatingFileHandler(
        filename=os.path.join(log_dir, "telegram_adapter.log"),
        maxBytes=8 * 1024 * 1024,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)


class TelegramAdapter:
    """Adapter for Telegram compatibility and semantic file envelopes."""

    def __init__(self, message_processor: "MessageProcessor"):
        self.message_processor = message_processor
        self.status = AdapterStatus(is_healthy=False)

    async def initialize(self):
        try:
            self.status.is_healthy = True
            logger.info("Telegram адаптер инициализирован")
        except Exception as error:
            logger.error("Ошибка инициализации Telegram адаптера: %s", error)
            self.status.is_healthy = False

    async def shutdown(self):
        logger.info("Telegram адаптер остановлен")
        self.status.is_healthy = False

    async def handle_unified_message(
        self,
        message: UnifiedMessage,
        progress_callback=None,
    ) -> UnifiedResponse:
        try:
            if not self.status.is_healthy:
                return UnifiedResponse(
                    message_id=message.id,
                    client_type=message.client_type,
                    content="Telegram адаптер не готов к работе",
                    response_type=MessageType.TEXT,
                )
            response = await self.message_processor.process_message(
                message,
                progress_callback=progress_callback,
            )
            self.status.last_activity = datetime.now()
            self.status.message_count += 1
            return response
        except Exception as error:
            logger.error(
                "Ошибка обработки сообщения Telegram: %s",
                error,
            )
            self.status.error_count += 1
            return UnifiedResponse(
                message_id=message.id,
                client_type=message.client_type,
                content=f"Произошла ошибка при обработке сообщения: {error}",
                response_type=MessageType.TEXT,
            )

    @staticmethod
    def build_input_envelope(
        *,
        bot_instance_id: str,
        update_id: str,
        chat_id: str,
        user_id: str,
        user_name: str | None,
        message_id: str,
        attachments: list[dict[str, Any]],
        caption: str | None = None,
        media_group_id: str | None = None,
        message_thread_id: str | int | None = None,
        reply_to_message_id: str | None = None,
        occurred_at: datetime | None = None,
        locale: str | None = None,
        admission_mode: InputAdmissionMode = InputAdmissionMode.AUTO,
        response_metadata: dict[str, Any] | None = None,
    ) -> ClientInputEnvelope:
        return build_telegram_input_envelope(
            bot_instance_id=bot_instance_id,
            update_id=update_id,
            chat_id=chat_id,
            user_id=user_id,
            user_name=user_name,
            message_id=message_id,
            attachments=attachments,
            caption=caption,
            media_group_id=media_group_id,
            message_thread_id=message_thread_id,
            reply_to_message_id=reply_to_message_id,
            occurred_at=occurred_at,
            locale=locale,
            admission_mode=admission_mode,
            response_metadata=response_metadata,
        )

    async def health_check(self) -> Dict[str, Any]:
        return {
            "healthy": self.status.is_healthy,
            "last_activity": (
                self.status.last_activity.isoformat()
                if self.status.last_activity
                else None
            ),
            "message_count": self.status.message_count,
            "error_count": self.status.error_count,
        }
