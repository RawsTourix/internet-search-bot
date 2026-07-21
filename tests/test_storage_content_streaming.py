import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.mcp.manager_context import ManagerToolContext
from src.mcp.mcp_client import SessionState
from src.planning.models import AgentActivity
from src.planning.runtime_context import (
    PlanningAwareContentStore,
    reset_manager_context,
    set_manager_context,
)
from src.runtime import ActiveAgentCycle
from src.storage import StorageConfigType, create_storage_services
from src.storage.errors import (
    StorageContentTooLargeError,
    StorageError,
    StorageValidationError,
)
from src.storage.streaming import StreamingFileSystemContentStore


class StreamingContentStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = StreamingFileSystemContentStore(
            StorageConfigType(root_dir=str(self.root))
        )

    def tearDown(self):
        self.temporary.cleanup()

    async def test_stream_round_trip_hash_and_incremental_text_size(self):
        text = "Привет, streaming content!"
        payload = text.encode("utf-8")

        async def chunks():
            boundaries = (1, 4, 9, len(payload))
            previous = 0
            for boundary in boundaries:
                yield payload[previous:boundary]
                previous = boundary

        ref = await self.store.save_stream(
            chunks(),
            source_type="user_file",
            source_name="note.txt",
            encoding="utf-8",
            max_size_bytes=len(payload),
        )
        metadata = await self.store.get_metadata(ref.content_id)

        restored = b"".join([
            chunk
            async for chunk in self.store.iter_content(
                ref.content_id,
                chunk_size=3,
            )
        ])
        self.assertEqual(restored, payload)
        self.assertEqual(metadata.size_chars, len(text))
        self.assertEqual(metadata.size_bytes, len(payload))
        self.assertEqual(
            metadata.content_hash,
            "sha256:" + hashlib.sha256(payload).hexdigest(),
        )
        self.assertEqual(metadata.mime_type, "text/plain")

    async def test_stream_failures_clean_partial_objects(self):
        async def too_large():
            yield b"123"
            yield b"456"

        with self.assertRaises(StorageContentTooLargeError):
            await self.store.save_stream(
                too_large(),
                source_type="user_file",
                max_size_bytes=5,
            )
        self.assertEqual(list((self.root / "contents").iterdir()), [])

        async def invalid_chunk():
            yield b"ok"
            yield "not bytes"

        with self.assertRaises(StorageValidationError):
            await self.store.save_stream(
                invalid_chunk(),
                source_type="user_file",
                max_size_bytes=100,
            )
        self.assertEqual(list((self.root / "contents").iterdir()), [])

        async def broken():
            yield b"partial"
            raise RuntimeError("producer failed")

        with self.assertRaises(StorageError):
            await self.store.save_stream(
                broken(),
                source_type="user_file",
                max_size_bytes=100,
            )
        self.assertEqual(list((self.root / "contents").iterdir()), [])

    async def test_stream_cancellation_cleans_temporary_object(self):
        first_chunk_seen = asyncio.Event()
        continue_producer = asyncio.Event()

        async def slow_chunks():
            yield b"first"
            first_chunk_seen.set()
            await continue_producer.wait()
            yield b"second"

        task = asyncio.create_task(
            self.store.save_stream(
                slow_chunks(),
                source_type="user_file",
                max_size_bytes=100,
            )
        )
        await first_chunk_seen.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(list((self.root / "contents").iterdir()), [])

    async def test_atomic_publish_failure_cleans_temporary_object(self):
        async def chunks():
            yield b"payload"

        with patch(
            "src.storage.streaming.os.replace",
            side_effect=OSError("injected"),
        ):
            with self.assertRaises(StorageError):
                await self.store.save_stream(
                    chunks(),
                    source_type="user_file",
                    max_size_bytes=100,
                )

        self.assertEqual(list((self.root / "contents").iterdir()), [])

    async def test_iter_content_bypasses_full_read_memory_limit(self):
        limited_root = self.root / "limited"
        store = StreamingFileSystemContentStore(
            StorageConfigType(
                root_dir=str(limited_root),
                max_in_memory_content_bytes=2,
            )
        )

        async def chunks():
            yield b"large"
            yield b"-payload"

        ref = await store.save_stream(
            chunks(),
            source_type="user_file",
            max_size_bytes=100,
        )

        with self.assertRaises(StorageContentTooLargeError):
            await store.read_content(ref.content_id)

        restored = b"".join([
            chunk
            async for chunk in store.iter_content(
                ref.content_id,
                chunk_size=2,
            )
        ])
        self.assertEqual(restored, b"large-payload")

        with self.assertRaises(StorageValidationError):
            await self._consume(
                store.iter_content(ref.content_id, chunk_size=0)
            )

    async def test_parallel_streams_have_unique_ids(self):
        async def save(index: int):
            async def chunks():
                yield f"value-{index}".encode()
            return await self.store.save_stream(
                chunks(),
                source_type="test",
                max_size_bytes=100,
            )

        refs = await asyncio.gather(*(save(index) for index in range(12)))
        self.assertEqual(
            len({ref.content_id for ref in refs}),
            len(refs),
        )

    @staticmethod
    async def _consume(iterator):
        return [chunk async for chunk in iterator]


class PlanningStreamingContentStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        services = create_storage_services(
            StorageConfigType(root_dir=str(self.root))
        )
        self.wrapped = services.content_store
        self.store = PlanningAwareContentStore(self.wrapped)
        self.cycle = ActiveAgentCycle(
            cycle_id="cycle-1",
            session_id="session-1",
            original_user_request="Process a file",
            messages_for_llm=[],
            cycle_trace=[],
            original_user_message_index=0,
            active_plan_id="plan_" + "a" * 32,
            active_plan_revision=3,
            active_plan_node_id="pnode_" + "b" * 32,
            activity=AgentActivity.PROCESSING,
        )
        self.context = ManagerToolContext(
            session_id="session-1",
            cycle_id="cycle-1",
            active_cycle=self.cycle,
            session_state=SessionState(),
        )

    def tearDown(self):
        self.temporary.cleanup()

    async def test_stream_preserves_plan_metadata_and_proxies_iteration(self):
        async def chunks():
            yield b"artifact"
            yield b"-input"

        token = set_manager_context(self.context)
        try:
            ref = await self.store.save_stream(
                chunks(),
                source_type="user_file",
                cycle_id="cycle-1",
                metadata={"client": "web"},
                max_size_bytes=100,
            )
        finally:
            reset_manager_context(token)

        metadata = await self.store.get_metadata(ref.content_id)
        self.assertEqual(metadata.metadata["client"], "web")
        self.assertEqual(
            metadata.metadata["plan_id"],
            self.cycle.active_plan_id,
        )
        self.assertEqual(metadata.metadata["plan_revision"], 3)
        self.assertEqual(
            metadata.metadata["plan_node_id"],
            self.cycle.active_plan_node_id,
        )
        self.assertEqual(
            metadata.metadata["agent_activity"],
            AgentActivity.PROCESSING.value,
        )

        restored = b"".join([
            chunk
            async for chunk in self.store.iter_content(
                ref.content_id,
                chunk_size=4,
            )
        ])
        self.assertEqual(restored, b"artifact-input")
