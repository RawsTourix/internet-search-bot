import asyncio
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.storage import StorageConfigType
from src.storage.errors import (
    StorageContentTooLargeError,
    StorageError,
    StorageIntegrityError,
    StorageNotFoundError,
    StorageSerializationError,
    StorageValidationError,
)
from src.storage.file_backend import FileSystemContentStore
from src.storage.models import new_content_id


class ContentStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = FileSystemContentStore(StorageConfigType(root_dir=str(self.root)))

    def tearDown(self):
        self.temporary.cleanup()

    async def test_text_round_trip(self):
        text = "Привет, storage!"
        ref = await self.store.save_content(
            text,
            source_type="tool_result",
            source_name="search",
            cycle_id="cycle-1",
            tool_call_id="call-1",
            metadata={"language": "ru"},
        )

        metadata = await self.store.get_metadata(ref.content_id)

        self.assertEqual(await self.store.read_text(ref.content_id), text)
        self.assertEqual(await self.store.read_content(ref.content_id), text.encode())
        self.assertEqual(ref.size_bytes, len(text.encode()))
        self.assertEqual(ref.size_chars, len(text))
        self.assertEqual(ref.mime_type, "text/plain")
        self.assertEqual(metadata.encoding, "utf-8")
        self.assertEqual(metadata.schema_version, 1)
        self.assertEqual(metadata.metadata, {"language": "ru"})
        self.assertEqual(
            ref.content_hash,
            "sha256:" + hashlib.sha256(text.encode()).hexdigest(),
        )
        self.assertFalse(any("path" in key for key in ref.model_fields))

    async def test_binary_round_trip_and_missing_encoding(self):
        content = b"\x00\xffbinary"
        ref = await self.store.save_content(content, source_type="user_file")

        self.assertEqual(await self.store.read_content(ref.content_id), content)
        self.assertEqual(ref.mime_type, "application/octet-stream")
        self.assertIsNone(ref.size_chars)
        with self.assertRaises(StorageValidationError):
            await self.store.read_text(ref.content_id)

    async def test_public_validation_and_serialization_errors_are_managed(self):
        with self.assertRaises(StorageValidationError):
            await self.store.save_content(b"data", source_type="")
        with self.assertRaises(StorageValidationError):
            await self.store.save_content(
                b"data",
                source_type="test",
                size_tokens_estimate=-1,
            )
        with self.assertRaises(StorageSerializationError):
            await self.store.save_content(
                b"data",
                source_type="test",
                metadata={"not_json": object()},
            )
        self.assertEqual(list((self.root / "contents").iterdir()), [])

    async def test_invalid_binary_encoding_preserves_bytes_and_size_chars_none(self):
        ref = await self.store.save_content(
            b"\xff",
            source_type="user_file",
            encoding="utf-8",
        )

        self.assertIsNone(ref.size_chars)
        self.assertEqual(await self.store.read_content(ref.content_id), b"\xff")
        with self.assertRaises(StorageValidationError):
            await self.store.read_text(ref.content_id)

    async def test_range_reading(self):
        ref = await self.store.save_content(b"0123456789", source_type="test")

        beginning = await self.store.read_range(ref.content_id, offset=0, length=3)
        middle = await self.store.read_range(ref.content_id, offset=4, length=3)
        ending = await self.store.read_range(ref.content_id, offset=8, length=8)
        beyond = await self.store.read_range(ref.content_id, offset=20, length=2)

        self.assertEqual(beginning.data, b"012")
        self.assertFalse(beginning.eof)
        self.assertEqual(middle.data, b"456")
        self.assertEqual(ending.data, b"89")
        self.assertTrue(ending.eof)
        self.assertEqual(beyond.data, b"")
        self.assertTrue(beyond.eof)
        for offset, length in ((-1, 1), (0, 0), (0, -1)):
            with self.subTest(offset=offset, length=length):
                with self.assertRaises(StorageValidationError):
                    await self.store.read_range(
                        ref.content_id,
                        offset=offset,
                        length=length,
                    )

    async def test_streaming_case_insensitive_search_and_limit(self):
        prefix = "я" * (64 * 1024 - 3)
        text = prefix + "Needle ещё needle и NEEDLE"
        ref = await self.store.save_content(text, source_type="test")

        matches = await self.store.search_text(ref.content_id, query="needle", limit=2)

        self.assertEqual(len(matches), 2)
        self.assertEqual(matches[0].char_start, len(prefix))
        self.assertLess(matches[0].char_start, matches[1].char_start)
        self.assertIn("Needle", matches[0].excerpt)

    async def test_memory_limit_blocks_full_reads_but_not_range_or_search(self):
        limited = FileSystemContentStore(
            StorageConfigType(
                root_dir=str(self.root / "limited"),
                max_in_memory_content_bytes=4,
            )
        )
        ref = await limited.save_content("hello needle", source_type="test")

        with self.assertRaises(StorageContentTooLargeError):
            await limited.read_content(ref.content_id)
        with self.assertRaises(StorageContentTooLargeError):
            await limited.read_text(ref.content_id)
        self.assertEqual(
            (await limited.read_range(ref.content_id, offset=0, length=5)).data,
            b"hello",
        )
        self.assertEqual(len(await limited.search_text(ref.content_id, query="needle")), 1)

    async def test_integrity_missing_binary_corrupt_metadata_and_schema(self):
        tampered = await self.store.save_content(b"original", source_type="test")
        self._object_file(tampered.content_id, "content.bin").write_bytes(b"changed!")
        with self.assertRaises(StorageIntegrityError):
            await self.store.read_content(tampered.content_id)

        missing = await self.store.save_content(b"data", source_type="test")
        self._object_file(missing.content_id, "content.bin").unlink()
        with self.assertRaises(StorageIntegrityError):
            await self.store.read_content(missing.content_id)

        corrupt = await self.store.save_content(b"data", source_type="test")
        self._object_file(corrupt.content_id, "metadata.json").write_text("{", "utf-8")
        with self.assertRaises(StorageSerializationError):
            await self.store.get_metadata(corrupt.content_id)

        unsupported = await self.store.save_content(b"data", source_type="test")
        metadata_path = self._object_file(unsupported.content_id, "metadata.json")
        payload = json.loads(metadata_path.read_text("utf-8"))
        payload["schema_version"] = 2
        metadata_path.write_text(json.dumps(payload), "utf-8")
        with self.assertRaises(StorageSerializationError):
            await self.store.get_metadata(unsupported.content_id)

    async def test_not_found_path_traversal_and_wrong_prefix(self):
        with self.assertRaises(StorageNotFoundError):
            await self.store.get_metadata(new_content_id())
        for invalid in ("../../escape", "art_" + "a" * 32):
            with self.subTest(invalid=invalid):
                with self.assertRaises(StorageValidationError):
                    await self.store.get_metadata(invalid)

    async def test_atomic_failure_cleans_temp_and_next_save_succeeds(self):
        with patch("src.storage.file_backend.os.replace", side_effect=OSError("injected")):
            with self.assertRaises(StorageError):
                await self.store.save_content(b"failed", source_type="test")

        self.assertEqual(list((self.root / "contents").iterdir()), [])
        successful = await self.store.save_content(b"ok", source_type="test")
        self.assertEqual(await self.store.read_content(successful.content_id), b"ok")

    async def test_parallel_saves_have_unique_ids_and_matching_metadata(self):
        refs = await asyncio.gather(
            *(
                self.store.save_content(f"value-{index}", source_type="test")
                for index in range(12)
            )
        )

        self.assertEqual(len({ref.content_id for ref in refs}), len(refs))
        for index, ref in enumerate(refs):
            self.assertEqual(await self.store.read_text(ref.content_id), f"value-{index}")

    def _object_file(self, content_id, filename):
        return self.root / "contents" / content_id / filename
