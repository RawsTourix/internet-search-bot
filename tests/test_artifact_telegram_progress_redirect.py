import unittest

from src.servers.telegram.progress_redirect import TelegramProgressRedirects


class TelegramProgressRedirectTests(unittest.TestCase):
    def test_resolves_relocation_chain_to_send_status(self):
        redirects = TelegramProgressRedirects()
        redirects.register(chat_id=1, old_message_id=10, new_message_id=11)
        redirects.register(chat_id=1, old_message_id=11, new_message_id=12)
        redirects.register(chat_id=1, old_message_id=12, new_message_id=20)

        self.assertEqual(
            redirects.resolve(chat_id=1, message_id=10),
            20,
        )
        self.assertEqual(
            redirects.resolve(chat_id=1, message_id=11),
            20,
        )
        self.assertEqual(
            redirects.resolve(chat_id=2, message_id=10),
            10,
        )

    def test_rejects_redirect_cycle(self):
        redirects = TelegramProgressRedirects()
        redirects.register(chat_id=1, old_message_id=10, new_message_id=11)
        with self.assertRaises(ValueError):
            redirects.register(chat_id=1, old_message_id=11, new_message_id=10)

    def test_storage_is_bounded(self):
        redirects = TelegramProgressRedirects(maximum_entries=2)
        redirects.register(chat_id=1, old_message_id=1, new_message_id=2)
        redirects.register(chat_id=1, old_message_id=2, new_message_id=3)
        redirects.register(chat_id=1, old_message_id=3, new_message_id=4)
        self.assertLessEqual(len(redirects), 2)


if __name__ == "__main__":
    unittest.main()
