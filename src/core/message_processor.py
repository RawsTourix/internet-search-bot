import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Any, Dict

from .models import ClientType, MessageType, UnifiedMessage, UnifiedResponse
from .response_metadata import agent_result_metadata
from .session_ids import resolve_message_session_id
from ..api.api import API
from ..api.session_reset import reset_runtime_session
from ..ingress import CommittedInputBatch, legacy_message_to_input_envelope
from ..localization.models import LocalizationMessage


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
    """Central compatibility boundary for messages and committed batches.

    Legacy text messages are normalized into the same durable ingress contract
    as files. Commands remain explicit control boundaries until the full
    ``v0.4-input-runtime`` admission/control layer replaces this wrapper.
    """

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
                session_id = self._build_session_id(message)
                envelope = legacy_message_to_input_envelope(message)
                submission = await API.submit_input(
                    envelope,
                    session_id=session_id,
                )
                response_metadata = {
                    "input_batch_id": submission.input_batch_id,
                    "input_state": submission.state,
                    "duplicate": submission.duplicate,
                    "ack_policy": submission.ack_policy.value,
                    "presentation_event": (
                        submission.presentation_event.model_dump(mode="json")
                        if submission.presentation_event is not None
                        else None
                    ),
                }
                presentation_text = self._render_submission(submission)
                if presentation_text is not None and (
                    submission.state == "collecting"
                    or submission.committed_batch is None
                    or submission.duplicate
                ):
                    response_content = presentation_text
                elif submission.state == "collecting":
                    response_content = (
                        "Сообщение добавлено к открытому пакету. Обработка "
                        "начнётся после завершения приёма всех его частей."
                    )
                elif submission.committed_batch is None:
                    response_content = (
                        "Сообщение принято, но входной пакет пока не готов к "
                        "обработке."
                    )
                elif submission.duplicate:
                    response_content = (
                        "Это сообщение уже было принято ранее; повторный запуск "
                        "агента пропущен."
                    )
                else:
                    result = await API.call_agent_batch(
                        submission.input_batch_id,
                        session_id=session_id,
                        progress_callback=progress_callback,
                        progress_locale=metadata.get("progress_locale", "ru"),
                    )
                    response_content = result.content
                    response_metadata.update(self._agent_result_metadata(result))
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
    def _render_message(message: LocalizationMessage, *, locale: str) -> str:
        service = getattr(
            getattr(API, "ingress_services", None),
            "localization_service",
            None,
        )
        if service is None:
            return message.message_key
        return service.render(message, locale=locale)

    def _render_submission(self, submission) -> str | None:
        event = submission.presentation_event
        if event is None:
            return None
        message = LocalizationMessage(
            message_key=(
                "input_batch.duplicate"
                if submission.duplicate
                else event.message_key
            ),
            params=event.params if not submission.duplicate else {},
            severity=event.severity,
        )
        return self._render_message(message, locale=event.locale)

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
                result = await reset_runtime_session(
                    API,
                    self._build_session_id(message),
                )
                if result.cancelled_input_batch_count:
                    return (
                        "✅ Память успешно очищена. "
                        "Незавершённых входных пакетов отменено: "
                        f"{result.cancelled_input_batch_count}."
                    )
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
