import unittest

from src.core.models import ClientType
from src.core.session_ids import resolve_message_session_id


class MessageProcessorSessionTests(unittest.TestCase):
    def test_fully_namespaced_telegram_session_is_authoritative(self):
        self.assertEqual(
            resolve_message_session_id(
                client_type=ClientType.TELEGRAM,
                metadata={
                    "session_id": "telegram:conversation:-1001:thread:9",
                    "chat_id": -1001,
                },
                user_id="user-1",
            ),
            "telegram:conversation:-1001:thread:9",
        )

    def test_legacy_unqualified_web_session_keeps_compatibility_prefix(self):
        self.assertEqual(
            resolve_message_session_id(
                client_type=ClientType.WEB,
                metadata={"session_id": "browser-session-1"},
                user_id="user-1",
            ),
            "web:session:browser-session-1",
        )

    def test_chat_fallback_is_unchanged(self):
        self.assertEqual(
            resolve_message_session_id(
                client_type=ClientType.TELEGRAM,
                metadata={"chat_id": 42},
                user_id="user-1",
            ),
            "telegram:chat:42",
        )

    def test_user_fallback_and_empty_values(self):
        self.assertEqual(
            resolve_message_session_id(
                client_type=ClientType.WEB,
                metadata={},
                user_id="user-1",
            ),
            "web:user:user-1",
        )
        with self.assertRaises(ValueError):
            resolve_message_session_id(
                client_type=ClientType.TELEGRAM,
                metadata={"session_id": "   "},
                user_id="user-1",
            )
        with self.assertRaises(ValueError):
            resolve_message_session_id(
                client_type=ClientType.TELEGRAM,
                metadata={},
                user_id="   ",
            )


if __name__ == "__main__":
    unittest.main()
