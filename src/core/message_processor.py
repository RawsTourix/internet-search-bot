import logging
import os
from collections import Counter
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any, Dict

from .models import ClientType, MessageType, UnifiedMessage, UnifiedResponse
from .response_metadata import agent_result_metadata
from .session_ids import resolve_message_session_id
from ..api.api import API
from ..api.session_reset import reset_runtime_session
from ..ingress import CommittedInputBatch, legacy_message_to_input_envelope
from ..input_runtime import InputAdmissionAction, InputAdmissionOutcome
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

    Every production committed batch now crosses the IR-3 admission boundary.
    Collection commands remain explicit ingress controls; additions accepted
    during a running cycle return immediately instead of starting a second one.
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
            content, metadata = await self._admit_and_route_batch(
                batch.input_batch_id,
                session_id=batch.session_id,
                progress_callback=progress_callback,
                progress_locale=progress_locale,
            )
            return UnifiedResponse(
                message_id=batch.input_batch_id,
                client_type=batch.client_type,
                content=content,
                response_type=MessageType.TEXT,
                metadata=metadata,
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

    async def _admit_and_route_batch(
        self,
        input_batch_id: str,
        *,
        session_id: str,
        progress_callback=None,
        progress_locale: str = "ru",
    ) -> tuple[str, dict[str, Any]]:
        """Admit first, then start/resume only when the outcome permits it."""
        input_runtime_config = getattr(API, "input_runtime_config", None)
        if input_runtime_config is not None and not input_runtime_config.enabled:
            result = await API.call_agent_batch(
                input_batch_id,
                session_id=session_id,
                progress_callback=progress_callback,
                progress_locale=progress_locale,
            )
            return result.content, self._agent_result_metadata(result)

        outcome = await API.admit_committed_batch(
            input_batch_id,
            session_id=session_id,
        )
        outcome_metadata = outcome.model_dump(
            mode="json",
            exclude={"admission"},
        )
        metadata: dict[str, Any] = {
            "input_batch_id": input_batch_id,
            "admission_outcome": outcome_metadata,
            **outcome_metadata,
        }

        result = None
        if outcome.should_start_runner:
            result = await API.start_admitted_cycle(
                outcome,
                progress_callback=progress_callback,
                progress_locale=progress_locale,
            )
        elif outcome.action in {
            InputAdmissionAction.RESUME_WAITING,
            InputAdmissionAction.DUPLICATE,
        } and outcome.should_wake_runner:
            result = await API.resume_admitted_cycle(
                outcome,
                progress_callback=progress_callback,
                progress_locale=progress_locale,
            )

        if result is not None:
            metadata.update(self._agent_result_metadata(result))
            return result.content, metadata
        return self._render_admission_outcome(
            outcome,
            locale=progress_locale,
        ), metadata

    @staticmethod
    def _render_admission_outcome(
        outcome: InputAdmissionOutcome,
        *,
        locale: str,
    ) -> str:
        english = locale.lower().strip().startswith("en")
        sequence = outcome.cycle_sequence or 0
        if outcome.action == InputAdmissionAction.QUEUED_RUNNING:
            return (
                f"Addition #{sequence} was accepted and is waiting to be applied."
                if english
                else f"Дополнение №{sequence} принято и ожидает применения."
            )
        if outcome.action == InputAdmissionAction.RESUME_WAITING:
            return (
                "The reply was accepted; resuming the current cycle was requested."
                if english
                else "Ответ принят; запрошено возобновление текущего цикла."
            )
        if outcome.action == InputAdmissionAction.QUEUED_PAUSED:
            return (
                "The addition was accepted while the cycle is paused."
                if english
                else "Дополнение принято во время паузы."
            )
        if outcome.action == InputAdmissionAction.RESUME_INTERRUPTED:
            return (
                "The addition was accepted; controlled cycle recovery was requested."
                if english
                else "Дополнение принято; запрошено управляемое восстановление цикла."
            )
        if outcome.action == InputAdmissionAction.CAPACITY_BLOCKED:
            return (
                "The additions queue is temporarily full. Please retry this committed batch."
                if english
                else "Очередь дополнений временно заполнена. Пакет можно повторно принять позже."
            )
        if outcome.action == InputAdmissionAction.DUPLICATE:
            return (
                "This input batch has already been accepted."
                if english
                else "Этот входной пакет уже был принят."
            )
        return "Request accepted." if english else "Запрос принят."

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
                else:
                    response_content, admission_metadata = (
                        await self._admit_and_route_batch(
                            submission.input_batch_id,
                            session_id=session_id,
                            progress_callback=progress_callback,
                            progress_locale=metadata.get(
                                "progress_locale",
                                "ru",
                            ),
                        )
                    )
                    response_metadata.update(admission_metadata)
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
            return await self._get_status_text(message)
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
/status - статус системы и текущей сессии
/reset - очистка памяти
/help - справка

Вы можете отправлять текст и файлы для обработки.
        """.strip()

    async def _get_status_text(self, message: UnifiedMessage) -> str:
        uptime = datetime.now() - self.stats["start_time"]
        session_id = self._build_session_id(message)
        now = datetime.now(timezone.utc)
        lines = [
            "Статус Gateway:",
            f"• Время работы: {str(uptime).split('.')[0]}",
            f"• Всего сообщений: {self.stats['total_messages']}",
            f"• Ошибок: {self.stats['errors']}",
            f"• Telegram: {self.stats['messages_by_client']['telegram']}",
            "",
            "Текущая сессия:",
            f"• ID: {session_id}",
        ]

        execution = await API.execution_coordinator.snapshot(session_id)
        lines.extend([
            f"• Execution: {execution.runtime_status}",
            "• Active cycle: " + (execution.active_cycle_id or "нет"),
            "• Active InputBatch: "
            + (execution.active_input_batch_id or "нет"),
            f"• В очереди batches: {execution.queued_batches}",
            "• Stop requested: "
            + ("да" if execution.stop_requested else "нет"),
        ])
        if execution.run_seconds is not None:
            lines.append(
                "• Время текущего цикла: "
                + self._format_duration(execution.run_seconds)
            )

        input_state = None
        input_repositories = getattr(API, "input_runtime_repositories", None)
        if input_repositories is not None:
            try:
                input_state = await input_repositories.sessions.get(session_id)
            except Exception:
                input_state = None
        if input_state is not None:
            lines.extend([
                f"• Input runtime: {input_state.cycle_status.value}",
                "• Accepted sequence: "
                f"{input_state.active_cycle_accepted_through_sequence}",
                "• Applied sequence: "
                f"{input_state.active_cycle_applied_through_sequence}",
            ])

        state = getattr(API.mcp_client, "session_states", {}).get(session_id)
        memory = getattr(API.mcp_client, "sessions", {}).get(session_id)
        if state is None:
            lines.append("• Runtime: idle (состояние ещё не создано)")
        else:
            status_value = getattr(state.status, "value", state.status)
            lines.append(f"• Runtime: {status_value}")
            lines.append(
                "• Ожидание пользователя: "
                + ("да" if state.awaiting_user_input else "нет")
            )
            lines.append(f"• Итераций последнего цикла: {state.iterations}")
            active_tool = getattr(state, "active_tool", None)
            lines.append(f"• Активный tool: {active_tool or 'нет'}")
            if state.last_error:
                lines.append(f"• Последняя ошибка: {state.last_error}")
        if memory is not None:
            lines.append(f"• Диалоговых ходов в памяти: {len(memory.dialog_turns)}")
            pending = getattr(memory, "pending_cycle", None)
            if pending is not None:
                lines.append(f"• Pending cycle: {pending.cycle_id}")

        collection_lines: list[str] = []
        collection_store = getattr(API.ingress_services, "collection_store", None)
        control_service = getattr(
            API.ingress_services,
            "draft_control_service",
            None,
        )
        if collection_store is not None and hasattr(collection_store, "list_active"):
            try:
                active = [
                    item
                    for item in await collection_store.list_active()
                    if item.scope.session_id == session_id
                ]
                reconciled = []
                for item in active:
                    current = (
                        await control_service.reconcile_collection(item)
                        if control_service is not None
                        else item
                    )
                    if current.is_active:
                        reconciled.append(current)
                if not reconciled:
                    collection_lines.append("• Сбор пакета: не активен")
                else:
                    collection = reconciled[0]
                    snapshot = await control_service.inspect(collection.scope)
                    opened = now - collection.opened_at
                    idle = now - collection.updated_at
                    collection_lines.extend([
                        f"• Сбор пакета: {collection.state.value}",
                        f"• Collection: {collection.collection_id}",
                        f"• InputBatch: {collection.bound_input_batch_id or 'не создан'}",
                        f"• Файлы: {snapshot.file_count}",
                        f"• Сообщения: {snapshot.text_part_count}",
                        f"• Возраст: {self._format_duration(opened.total_seconds())}",
                        f"• Без активности: {self._format_duration(idle.total_seconds())}",
                    ])
            except Exception as error:
                collection_lines.append(
                    f"• Сбор пакета: диагностика недоступна ({type(error).__name__})"
                )
        else:
            collection_lines.append("• Сбор пакета: control plane недоступен")
        lines.extend(["", "Входные пакеты:", *collection_lines])

        try:
            drafts = await API.ingress_services.batch_store.list_open_drafts(
                session_id=session_id
            )
            states = Counter(item.state.value for item in drafts)
            summary = ", ".join(
                f"{key}={value}" for key, value in sorted(states.items())
            ) or "нет"
            lines.append(f"• Открытых drafts: {len(drafts)} ({summary})")
        except Exception as error:
            lines.append(
                f"• Открытые drafts: недоступно ({type(error).__name__})"
            )

        try:
            presentations = await API.ingress_services.presentation_store.list_recoverable()
            session_presentations = [
                item for item in presentations
                if getattr(item, "session_id", None) == session_id
            ]
            lines.append(
                "• Recoverable presentations: "
                f"{len(session_presentations)} в сессии / {len(presentations)} всего"
            )
        except Exception as error:
            lines.append(
                f"• Recoverable presentations: недоступно ({type(error).__name__})"
            )

        try:
            outputs = await API.output_store.list_recoverable()
            session_outputs = [
                item for item in outputs
                if getattr(item, "session_id", None) == session_id
            ]
            output_states = Counter(item.state.value for item in session_outputs)
            output_summary = ", ".join(
                f"{key}={value}"
                for key, value in sorted(output_states.items())
            ) or "нет"
            lines.append(
                f"• Recoverable outputs: {len(session_outputs)} ({output_summary})"
            )
        except Exception as error:
            lines.append(
                f"• Recoverable outputs: недоступно ({type(error).__name__})"
            )

        trace_enabled = bool(
            getattr(API.artifact_config, "trace_enabled", False)
        )
        lines.extend([
            "",
            "Артефакты:",
            "• Lifecycle trace: " + ("включён" if trace_enabled else "выключен"),
            "",
            "Примечание: Telegram session привязана к chat/thread и сохраняет "
            "тот же ID после перезапуска процесса.",
        ])
        return "\n".join(lines)

    @staticmethod
    def _format_duration(seconds: float) -> str:
        total = max(0, int(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours}ч {minutes}м {secs}с"
        if minutes:
            return f"{minutes}м {secs}с"
        return f"{secs}с"

    async def get_stats(self) -> Dict[str, Any]:
        uptime = datetime.now() - self.stats["start_time"]
        return {
            **self.stats,
            "uptime_seconds": uptime.total_seconds(),
            "active_sessions": len(self.active_sessions),
        }
