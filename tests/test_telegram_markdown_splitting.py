import unittest

from src.utils.telegram_formatting import split_markdown_for_telegram


class TelegramMarkdownSplittingTests(unittest.TestCase):
    def test_long_single_paragraph_never_exceeds_limit(self):
        chunks = split_markdown_for_telegram("word " * 100, limit=25)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(0 < len(chunk) <= 25 for chunk in chunks))
        self.assertEqual(
            " ".join(" ".join(chunks).split()),
            " ".join(("word " * 100).split()),
        )

    def test_large_fenced_code_block_keeps_balanced_fences(self):
        text = "```python\n" + ("print('x')\n" * 20) + "```"
        chunks = split_markdown_for_telegram(text, limit=60)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 60 for chunk in chunks))
        self.assertTrue(all(chunk.startswith("```python\n") for chunk in chunks))
        self.assertTrue(all(chunk.endswith("\n```") for chunk in chunks))

    def test_invalid_limit_is_rejected(self):
        for value in (0, -1, True):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    split_markdown_for_telegram("text", limit=value)


if __name__ == "__main__":
    unittest.main()
