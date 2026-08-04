import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from src.artifacts import (
    ArtifactDeliveryRecord,
    ArtifactDeliveryState,
    ArtifactTraceService,
    FileSystemArtifactTraceStore,
    new_artifact_delivery_id,
    new_artifact_id,
    new_artifact_lineage_id,
)
from src.artifacts.advanced_delivery import AdvancedFileSystemArtifactDeliveryStore
from src.storage import StorageConfigType
from src.storage.models import new_content_id


class ArtifactTracingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.storage_config = StorageConfigType(
            root_dir=str(self.root / "storage")
        )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_jsonl_trace_is_session_scoped_and_redacts_sensitive_fields(self):
        store = FileSystemArtifactTraceStore(self.storage_config)
        service = ArtifactTraceService(store, max_string_chars=256)

        event = await service.record(
            session_id="telegram:conversation:12345",
            event_type="artifact_ingress_stored",
            stage="ingress",
            status="succeeded",
            direction="inbound",
            correlation={"input_batch_id": "ibat_example"},
            transport={
                "client_type": "telegram",
                "client_instance_id": "bot-1",
                "conversation_id": "12345",
                "source_message_id": "50",
                "token": "must-not-be-persisted",
            },
            artifact={
                "artifact_id": new_artifact_id(),
                "artifact_lineage_id": new_artifact_lineage_id(),
                "content_id": new_content_id(),
                "filename": "summary.md",
                "format_id": "markdown",
                "mime_type": "text/markdown",
                "size_bytes": 42,
                "content_hash": "sha256:" + "a" * 64,
                "purpose": "input",
                "version": 1,
                "local_path": "C:/secret/workspace/summary.md",
            },
            data={
                "authorization": "Bearer secret",
                "safe": "visible",
            },
        )

        self.assertIsNotNone(event)
        persisted = await store.list_session("telegram:conversation:12345")
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0].event_type, "artifact_ingress_stored")
        self.assertEqual(persisted[0].transport.client_type, "telegram")
        self.assertEqual(persisted[0].artifact.filename, "summary.md")
        self.assertEqual(persisted[0].data, {"safe": "visible"})
        self.assertEqual(
            await store.list_session("telegram:conversation:other"),
            [],
        )

        session_dirs = list(store.root.glob("session_*"))
        self.assertEqual(len(session_dirs), 1)
        raw = "\n".join(
            path.read_text(encoding="utf-8")
            for path in session_dirs[0].glob("*.jsonl")
        )
        self.assertNotIn("must-not-be-persisted", raw)
        self.assertNotIn("Bearer secret", raw)
        self.assertNotIn("C:/secret/workspace", raw)
        self.assertIn("summary.md", raw)

    async def test_rotation_preserves_append_order_and_accepts_large_single_event(self):
        store = FileSystemArtifactTraceStore(
            self.storage_config,
            max_file_bytes=1024,
        )
        service = ArtifactTraceService(store, max_string_chars=4000)
        start = datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc)

        for index in range(4):
            recorded = await service.record(
                session_id="web:conversation:rotation",
                event_type=f"event_{index}",
                stage="test",
                status="observed",
                occurred_at=start + timedelta(seconds=index),
                data={"payload": str(index) * 1800},
            )
            self.assertIsNotNone(recorded)

        persisted = await store.list_session("web:conversation:rotation")
        self.assertEqual(
            [item.event_type for item in persisted],
            ["event_0", "event_1", "event_2", "event_3"],
        )
        session_dir = store._session_dir("web:conversation:rotation")
        parts = sorted(session_dir.glob("*.jsonl"))
        self.assertGreaterEqual(len(parts), 4)
        for path in parts:
            for line in path.read_text(encoding="utf-8").splitlines():
                json.loads(line)

    async def test_trace_failure_is_best_effort(self):
        failing_store = AsyncMock()
        failing_store.append.side_effect = OSError("disk unavailable")
        service = ArtifactTraceService(failing_store)

        result = await service.record(
            session_id="telegram:conversation:1",
            event_type="artifact_ingress_stored",
            stage="ingress",
            status="succeeded",
        )

        self.assertIsNone(result)
        failing_store.append.assert_awaited_once()

    async def test_delivery_trace_is_written_after_durable_transitions(self):
        trace_store = FileSystemArtifactTraceStore(self.storage_config)
        trace_service = ArtifactTraceService(trace_store)
        delivery_store = AdvancedFileSystemArtifactDeliveryStore(
            self.storage_config,
            trace_service=trace_service,
        )
        now = datetime.now(timezone.utc)
        record = ArtifactDeliveryRecord(
            delivery_id=new_artifact_delivery_id(),
            session_id="telegram:conversation:delivery",
            cycle_id="cycle-delivery",
            artifact_id=new_artifact_id(),
            artifact_lineage_id=new_artifact_lineage_id(),
            content_id=new_content_id(),
            filename="result.md",
            format_id="markdown",
            mime_type="text/markdown",
            size_bytes=10,
            content_hash="sha256:" + "b" * 64,
            client_type="telegram",
            selection_index=0,
            state=ArtifactDeliveryState.SELECTED,
            created_at=now,
            updated_at=now,
        )

        selected = await delivery_store.select(record)
        repeated_selection = await delivery_store.select(record)
        self.assertEqual(repeated_selection.delivery_id, selected.delivery_id)
        delivering = await delivery_store.transition(
            selected.delivery_id,
            target=ArtifactDeliveryState.DELIVERING,
            allowed_from={ArtifactDeliveryState.SELECTED},
        )
        delivered = await delivery_store.transition(
            delivering.delivery_id,
            target=ArtifactDeliveryState.DELIVERED,
            allowed_from={ArtifactDeliveryState.DELIVERING},
            receipt={"transport": "telegram", "message_id": 77},
        )
        repeated_delivery = await delivery_store.transition(
            delivered.delivery_id,
            target=ArtifactDeliveryState.DELIVERED,
            allowed_from={ArtifactDeliveryState.DELIVERING},
            receipt={"transport": "telegram", "message_id": 77},
        )

        self.assertEqual(repeated_delivery.state, ArtifactDeliveryState.DELIVERED)
        events = await trace_store.list_session(
            "telegram:conversation:delivery"
        )
        self.assertEqual(
            [item.event_type for item in events],
            [
                "artifact_delivery_selected",
                "artifact_delivery_started",
                "artifact_delivery_succeeded",
            ],
        )
        self.assertEqual(events[-1].artifact.filename, "result.md")
        self.assertEqual(events[-1].data["state"], "delivered")


if __name__ == "__main__":
    unittest.main()
