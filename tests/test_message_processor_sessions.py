import unittest
from datetime import datetime

from src.core.message_processor import MessageProcessor
from src.core.models import ClientType, MessageType, UnifiedMessage


class MessageProcessorSessionTests(unittest.TestCase):
    def setUp(self):
        self.processor = MessageProcessor()

    def _message(self, *, client_type, metadata):
        return UnifiedMessage(
            id="message-1",
            timestamp=datetime.now(),
            client_type=client_type,
            message_type=MessageType.TEXT,
            content="hello",
            user_id="user-1",
            metadata=metadata,
        )

    def test_fully_namespaced_telegram_session_is_authoritative(self):
        message = self._message(
            client_type=ClientType.TELEGRAM,
            metadata={
                "session_id": "telegram:conversation:-1001:thread:9",
                "chat_id": -1001,
            },
        )
        self.assertEqual(
            self.processor._build_session_id(message),
            "telegram:conversation:-1001:thread:9",
        )

    def test_legacy_unqualified_web_session_keeps_compatibility_prefix(self):
        message = self._message(
            client_type=ClientType.WEB,
            metadata={"session_id": "browser-session-1"},
        )
        self.assertEqual(
            self.processor._build_session_id(message),
            "web:session:browser-session-1",
        )

    def test_chat_fallback_is_unchanged(self):
        message = self._message(
            client_type=ClientType.TELEGRAM,
            metadata={"chat_id": 42},
        )
        self.assertEqual(
            self.processor._build_session_id(message),
            "telegram:chat:42",
        )

    def test_empty_explicit_session_is_rejected(self):
        message = self._message(
            client_type=ClientType.TELEGRAM,
            metadata={"session_id": "   "},
        )
        with self.assertRaises(ValueError):
            self.processor._build_session_id(message)


if __name__ == "__main__":
    unittest.main()
