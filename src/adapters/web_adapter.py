import os
import logging
from typing import Dict, Any
from datetime import datetime
from logging.handlers import RotatingFileHandler

from ..core.models import UnifiedMessage, UnifiedResponse, MessageType, AdapterStatus
from ..core.message_processor import MessageProcessor


log_dir = "logging"
if not os.path.exists(log_dir):
    os.makedirs(log_dir, exist_ok=True)

formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

logger = logging.getLogger("WebAdapter")
logger.setLevel(logging.DEBUG)

file_handler = RotatingFileHandler(
    filename=os.path.join(log_dir, "web_adapter.log"),
    maxBytes=8 * 1024 * 1024,
    encoding="utf-8"
)
file_handler.setFormatter(formatter)

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(formatter)

logger.addHandler(file_handler)
logger.addHandler(console_handler)


class WebAdapter:
    """Адаптер для веб-интерфейса."""

    def __init__(self, message_processor: MessageProcessor):
        self.message_processor = message_processor
        self.status = AdapterStatus(is_healthy=False)

    async def initialize(self):
        try:
            self.status.is_healthy = True
            logger.info("Web адаптер инициализирован")
        except Exception as e:
            logger.error(f"Ошибка инициализации Web адаптера: {e}")
            self.status.is_healthy = False

    async def shutdown(self):
        logger.info("Web адаптер остановлен")

    async def handle_unified_message(
        self,
        message: UnifiedMessage,
        progress_callback=None,
    ) -> UnifiedResponse:
        try:
            if not self.status.is_healthy:
                logger.warning("Web адаптер не готов к работе")
                return UnifiedResponse(
                    message_id=message.id,
                    client_type=message.client_type,
                    content="Web адаптер не готов к работе",
                    response_type=MessageType.TEXT
                )

            response = await self.message_processor.process_message(
                message,
                progress_callback=progress_callback,
            )

            self.status.last_activity = datetime.now()
            self.status.message_count += 1

            return response

        except Exception as e:
            logger.error(f"Ошибка обработки web-сообщения: {e}")
            self.status.error_count += 1

            return UnifiedResponse(
                message_id=message.id,
                client_type=message.client_type,
                content=f"Произошла ошибка при обработке web-сообщения: {str(e)}",
                response_type=MessageType.TEXT
            )

    async def health_check(self) -> Dict[str, Any]:
        return {
            "healthy": self.status.is_healthy,
            "last_activity": self.status.last_activity.isoformat() if self.status.last_activity else None,
            "message_count": self.status.message_count,
            "error_count": self.status.error_count
        }
