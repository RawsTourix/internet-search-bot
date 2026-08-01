import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src.core.models import (
    AgentStatus,
    ClientType,
    MessageType,
    UnifiedMessage,
)
from src.core import message_processor as message_processor_module
from src.core.message_processor import MessageProcessor
from src.mcp.mcp_client import SessionState


class StatusDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _message() -> UnifiedMessage:
        return UnifiedMessage(
            id="status-message",
            client_type=ClientType.TELEGRAM,
            message_type=MessageType.COMMAND,
            content="/status",
            user_id="user-1",
            user_name="User",
            timestamp=datetime.now(timezone.utc),
            metadata={
                "chat_id": "chat-1",
                "session_id": "telegram:conversation:chat-1",
            },
            command="/status",
            arguments=[],
        )

    async def test_status_includes_runtime_collection_and_recovery_state(self):
        session_id = "telegram:conversation:chat-1"
        now = datetime.now(timezone.utc)
        scope = SimpleNamespace(session_id=session_id)
        collection = SimpleNamespace(
            collection_id="icol_status",
            scope=scope,
            state=SimpleNamespace(value="collecting"),
            bound_input_batch_id="ibat_status",
            opened_at=now - timedelta(minutes=3),
            updated_at=now - timedelta(seconds=15),
            is_active=True,
        )
        control = SimpleNamespace(
            reconcile_collection=AsyncMock(return_value=collection),
            inspect=AsyncMock(return_value=SimpleNamespace(
                file_count=7,
                text_part_count=2,
            )),
        )
        ingress_services = SimpleNamespace(
            collection_store=SimpleNamespace(
                list_active=AsyncMock(return_value=[collection])
            ),
            draft_control_service=control,
            batch_store=SimpleNamespace(
                list_open_drafts=AsyncMock(return_value=[
                    SimpleNamespace(state=SimpleNamespace(value="collecting"))
                ])
            ),
            presentation_store=SimpleNamespace(
                list_recoverable=AsyncMock(return_value=[
                    SimpleNamespace(session_id=session_id),
                    SimpleNamespace(session_id="another-session"),
                ])
            ),
        )
        fake_api = SimpleNamespace(
            mcp_client=SimpleNamespace(
                session_states={
                    session_id: SessionState(
                        status=AgentStatus.WAITING_USER,
                        iterations=4,
                        awaiting_user_input=True,
                    )
                },
                sessions={},
            ),
            ingress_services=ingress_services,
            output_store=SimpleNamespace(
                list_recoverable=AsyncMock(return_value=[
                    SimpleNamespace(
                        session_id=session_id,
                        state=SimpleNamespace(value="ready"),
                    )
                ])
            ),
            artifact_config=SimpleNamespace(trace_enabled=True),
        )

        processor = MessageProcessor()
        with patch.object(message_processor_module, "API", fake_api):
            text = await processor._get_status_text(self._message())

        self.assertIn(f"• ID: {session_id}", text)
        self.assertIn("• Runtime: waiting_user", text)
        self.assertIn("• Ожидание пользователя: да", text)
        self.assertIn("• Сбор пакета: collecting", text)
        self.assertIn("• Collection: icol_status", text)
        self.assertIn("• InputBatch: ibat_status", text)
        self.assertIn("• Файлы: 7", text)
        self.assertIn("• Сообщения: 2", text)
        self.assertIn("• Открытых drafts: 1 (collecting=1)", text)
        self.assertIn("1 в сессии / 2 всего", text)
        self.assertIn("• Recoverable outputs: 1 (ready=1)", text)
        self.assertIn("• Lifecycle trace: включён", text)
        self.assertIn("сохраняет тот же ID после перезапуска", text)

    async def test_status_degrades_per_section_instead_of_failing_command(self):
        session_id = "telegram:conversation:chat-1"
        unavailable = AsyncMock(side_effect=OSError("store unavailable"))
        fake_api = SimpleNamespace(
            mcp_client=SimpleNamespace(session_states={}, sessions={}),
            ingress_services=SimpleNamespace(
                collection_store=SimpleNamespace(list_active=unavailable),
                draft_control_service=SimpleNamespace(),
                batch_store=SimpleNamespace(list_open_drafts=unavailable),
                presentation_store=SimpleNamespace(list_recoverable=unavailable),
            ),
            output_store=SimpleNamespace(list_recoverable=unavailable),
            artifact_config=SimpleNamespace(trace_enabled=False),
        )

        processor = MessageProcessor()
        with patch.object(message_processor_module, "API", fake_api):
            text = await processor._get_status_text(self._message())

        self.assertIn("Статус Gateway:", text)
        self.assertIn("диагностика недоступна (OSError)", text)
        self.assertIn("Открытые drafts: недоступно (OSError)", text)
        self.assertIn("Recoverable presentations: недоступно (OSError)", text)
        self.assertIn("Recoverable outputs: недоступно (OSError)", text)
        self.assertIn("• Lifecycle trace: выключен", text)


if __name__ == "__main__":
    unittest.main()
