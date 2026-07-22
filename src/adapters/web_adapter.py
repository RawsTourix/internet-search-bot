import os
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Dict

from ..core.models import (
    UnifiedMessage,
    UnifiedResponse,
    MessageType,
    AdapterStatus,
    ClientType,
)
from ..core.message_processor import MessageProcessor
from ..ingress import (
    ClientConversationRef,
    ClientInputEnvelope,
    ClientResponseRoute,
    ClientSenderRef,
    IngressAttachmentSlot,
    IngressTextPart,
    InputAdmissionMode,
)


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
    """Adapter for text compatibility and atomic Web input envelopes."""

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
        self.status.is_healthy = False

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

    @staticmethod
    def build_input_envelope(
        *,
        idempotency_key: str,
        client_instance_id: str,
        session_id: str,
        user_id: str,
        source_message_id: str,
        text: str | None,
        attachments: list[dict[str, Any]],
        admission_mode: InputAdmissionMode = InputAdmissionMode.AUTO,
        locale: str | None = None,
        occurred_at: datetime | None = None,
        response_metadata: dict[str, Any] | None = None,
    ) -> ClientInputEnvelope:
        """Build semantic Web input; bytes stay in multipart upload streams."""
        attachment_slots = [
            IngressAttachmentSlot(
                slot_id=str(item["slot_id"]),
                media_kind=str(item.get("media_kind") or "document"),
                original_filename=item.get("filename"),
                declared_mime_type=item.get("mime_type"),
                declared_size_bytes=item.get("size_bytes"),
                upload_field_name=str(item["upload_field_name"]),
                metadata=dict(item.get("metadata") or {}),
            )
            for item in attachments
        ]
        text_parts = []
        if text and text.strip():
            text_parts.append(IngressTextPart(
                part_id="message_text",
                kind="message_text",
                text=text,
                attachment_slot_ids=[
                    item.slot_id for item in attachment_slots
                ],
            ))
        return ClientInputEnvelope(
            idempotency_key=idempotency_key,
            client_type=ClientType.WEB,
            client_instance_id=client_instance_id,
            conversation=ClientConversationRef(
                conversation_id=session_id,
            ),
            sender=ClientSenderRef(principal_id=user_id),
            source_message_id=source_message_id,
            occurred_at=occurred_at or datetime.now(timezone.utc),
            text_parts=text_parts,
            attachment_slots=attachment_slots,
            locale=locale,
            admission_mode=admission_mode,
            response_route=ClientResponseRoute(
                route_type="web",
                conversation_id=session_id,
                reply_to_message_id=source_message_id,
                metadata=dict(response_metadata or {}),
            ),
        )

    async def health_check(self) -> Dict[str, Any]:
        return {
            "healthy": self.status.is_healthy,
            "last_activity": self.status.last_activity.isoformat() if self.status.last_activity else None,
            "message_count": self.status.message_count,
            "error_count": self.status.error_count
        }
