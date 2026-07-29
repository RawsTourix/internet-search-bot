import tempfile
import unittest

from telegram import InputMediaDocument

from src.servers.telegram.output_batch_gateway import (
    TelegramClaimedOutputGateway,
)


class TelegramMediaGroupAttachMappingTests(unittest.TestCase):
    def test_claimed_streams_receive_unique_attach_uris(self):
        first = tempfile.SpooledTemporaryFile(mode="w+b")
        second = tempfile.SpooledTemporaryFile(mode="w+b")
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        first.write(b"one")
        second.write(b"two")
        first.seek(0)
        second.seek(0)

        first_file = TelegramClaimedOutputGateway.telegram_input_file(
            first,
            "one.txt",
        )
        second_file = TelegramClaimedOutputGateway.telegram_input_file(
            second,
            "two.txt",
        )
        media = [
            InputMediaDocument(media=first_file),
            InputMediaDocument(media=second_file),
        ]

        self.assertEqual(media[0].media.filename, "one.txt")
        self.assertEqual(media[1].media.filename, "two.txt")
        self.assertIs(media[0].media.input_file_content, first)
        self.assertIs(media[1].media.input_file_content, second)
        self.assertTrue(media[0].media.attach_uri.startswith("attach://attached"))
        self.assertTrue(media[1].media.attach_uri.startswith("attach://attached"))
        self.assertNotEqual(media[0].media.attach_uri, media[1].media.attach_uri)

    def test_attached_stream_remains_lazy(self):
        spool = tempfile.SpooledTemporaryFile(mode="w+b")
        self.addCleanup(spool.close)
        spool.write(b"payload")
        spool.seek(0)

        input_file = TelegramClaimedOutputGateway.telegram_input_file(
            spool,
            "payload.bin",
        )

        self.assertIs(input_file.input_file_content, spool)
        self.assertEqual(input_file.field_tuple[0], "payload.bin")
        self.assertEqual(input_file.attach_uri, f"attach://{input_file.attach_name}")


if __name__ == "__main__":
    unittest.main()
