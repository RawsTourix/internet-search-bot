import os
import logging
import re
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Dict

from ..core.models import UnifiedMessage, UnifiedResponse, ClientType, MessageType, AdapterStatus
from ..core.message_processor import MessageProcessor
from ..ingress import (
    ClientAttachmentLocator,
    ClientConversationRef,
    ClientInputEnvelope,
    ClientResponseRoute,
    ClientSenderRef,
    IngressAttachmentSlot,
    IngressTextPart,
    InputAdmissionMode,
)

# Проверяем и создаем папку для логов
log_dir = "logging"
if not os.path.exists(log_dir):
    os.makedirs(log_dir, exist_ok=True)

# Настройка логирования
formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

logger = logging.getLogger("TelegramAdapter")
logger.setLevel(logging.DEBUG)

file_handler = RotatingFileHandler(
    filename=os.path.join(log_dir, "telegram_adapter.log"),
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


class TelegramAdapter:
    """Adapter for Telegram compatibility and semantic file envelopes."""
    
    def __init__(self, message_processor: MessageProcessor):
        self.message_processor = message_processor
        self.status = AdapterStatus(is_healthy=False)
        
    async def initialize(self):
        """Инициализация Telegram адаптера"""
        try:
            self.status.is_healthy = True
            logger.info("Telegram адаптер инициализирован")
            
        except Exception as e:
            logger.error(f"Ошибка инициализации Telegram адаптера: {e}")
            self.status.is_healthy = False
    
    async def shutdown(self):
        """Остановка Telegram адаптера"""
        logger.info("Telegram адаптер остановлен")
        self.status.is_healthy = False
    
    async def handle_unified_message(
        self,
        message: UnifiedMessage,
        progress_callback=None,
    ) -> UnifiedResponse:
        """Обработка унифицированного сообщения от Telegram-сервера"""
        try:
            if not self.status.is_healthy:
                logger.warning("Telegram адаптер не готов к работе")
                return UnifiedResponse(
                    message_id=message.id,
                    client_type=message.client_type,
                    content="Telegram адаптер не готов к работе",
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
            logger.error(f"Ошибка обработки унифицированного сообщения от Telegram-сервера: {e}")
            self.status.error_count += 1
            
            return UnifiedResponse(
                message_id=message.id,
                client_type=message.client_type,
                content=f"Произошла ошибка при обработке сообщения: {str(e)}",
                response_type=MessageType.TEXT
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
        message_thread_id: str | None = None,
        reply_to_message_id: str | None = None,
        occurred_at: datetime | None = None,
        locale: str | None = None,
        admission_mode: InputAdmissionMode = InputAdmissionMode.AUTO,
        response_metadata: dict[str, Any] | None = None,
    ) -> ClientInputEnvelope:
        """Build an envelope with opaque Telegram file locators, never file URLs."""
        source_message = str(message_id)
        slots: list[IngressAttachmentSlot] = []
        for index, item in enumerate(attachments, start=1):
            file_id = str(item["file_id"])
            slot_suffix = re.sub(
                r"[^a-zA-Z0-9_.-]",
                "-",
                f"{source_message}-{index}",
            )[:90]
            slots.append(IngressAttachmentSlot(
                slot_id=f"slot_{slot_suffix}",
                media_kind=str(item.get("media_kind") or "document"),
                original_filename=item.get("filename"),
                declared_mime_type=item.get("mime_type"),
                declared_size_bytes=item.get("size_bytes"),
                transport_locator=ClientAttachmentLocator(
                    provider="telegram",
                    locator=file_id,
                ),
                metadata={
                    "file_unique_id": item.get("file_unique_id"),
                    "telegram_media_group_id": media_group_id,
                },
            ))

        text_parts = []
        if caption and caption.strip():
            text_parts.append(IngressTextPart(
                part_id=f"caption-{source_message}",
                kind="caption",
                text=caption,
                attachment_slot_ids=[slot.slot_id for slot in slots],
            ))

        progress_metadata = dict(response_metadata or {})
        progress_metadata.setdefault("chat_id", chat_id)
        progress_metadata.setdefault("message_id", message_id)

        return ClientInputEnvelope(
            idempotency_key=(
                f"telegram:{bot_instance_id}:update:{update_id}:message:{message_id}"
            ),
            client_type=ClientType.TELEGRAM,
            client_instance_id=bot_instance_id,
            conversation=ClientConversationRef(
                conversation_id=str(chat_id),
                thread_id=(
                    str(message_thread_id)
                    if message_thread_id is not None
                    else None
                ),
            ),
            sender=ClientSenderRef(
                principal_id=str(user_id),
                display_name=user_name,
            ),
            source_update_id=str(update_id),
            source_message_id=source_message,
            source_group_id=(
                str(media_group_id) if media_group_id is not None else None
            ),
            reply_to_message_id=(
                str(reply_to_message_id)
                if reply_to_message_id is not None
                else None
            ),
            occurred_at=occurred_at or datetime.now(timezone.utc),
            text_parts=text_parts,
            attachment_slots=slots,
            locale=locale,
            admission_mode=admission_mode,
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id=str(chat_id),
                thread_id=(
                    str(message_thread_id)
                    if message_thread_id is not None
                    else None
                ),
                reply_to_message_id=source_message,
                metadata=progress_metadata,
            ),
            metadata={
                "grouping_hint": (
                    "media_group" if media_group_id is not None
                    else "standalone_attachment"
                ),
            },
        )
    
    async def health_check(self) -> Dict[str, Any]:
        """Проверка здоровья адаптера"""
        return {
            "healthy": self.status.is_healthy,
            "last_activity": self.status.last_activity.isoformat() if self.status.last_activity else None,
            "message_count": self.status.message_count,
            "error_count": self.status.error_count
        }
