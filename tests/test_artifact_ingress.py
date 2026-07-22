import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.artifacts import ArtifactConfigType, create_artifact_services
from src.core.models import ClientType
from src.ingress import (
    ClientConversationRef,
    ClientInputEnvelope,
    ClientResponseRoute,
    ClientSenderRef,
    IngressAttachmentSlot,
    IngressConfigType,
    IngressConflictError,
    IngressTextPart,
    InputBatchDraftState,
    create_ingress_services,
)
from src.storage import StorageConfigType, create_storage_services


async def chunks(*values: bytes):
    for value in values:
        yield value


class ArtifactIngressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.storage_config = StorageConfigType(root_dir=str(root / "storage"))
        self.storage = create_storage_services(self.storage_config)
        self.artifacts = create_artifact_services(
            storage_config=self.storage_config,
            artifact_config=ArtifactConfigType(
                max_artifact_size_bytes=1024 * 1024,
                max_patchable_text_bytes=1024 * 1024,
                max_workspace_bytes=2 * 1024 * 1024,
            ),
            content_store=self.storage.content_store,
        )
        self.ingress = create_ingress_services(
            storage_config=self.storage_config,
            ingress_config=IngressConfigType(
                max_batch_total_bytes=2 * 1024 * 1024,
            ),
            content_store=self.storage.content_store,
            artifact_services=self.artifacts,
        )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    def _envelope(
        self,
        *,
        key: str = "telegram:bot-1:update-1",
        text: str | None = "Please inspect the file",
        attachment: bool = True,
        filename: str = "report.md",
    ) -> ClientInputEnvelope:
        slots = []
        if attachment:
            slots.append(IngressAttachmentSlot(
                slot_id="slot_file_1",
                media_kind="document",
                original_filename=filename,
                declared_mime_type="text/markdown",
                declared_size_bytes=11,
                upload_field_name="file_1",
            ))
        text_parts = []
        if text is not None:
            text_parts.append(IngressTextPart(
                part_id="text-1",
                kind="caption" if attachment else "message_text",
                text=text,
                attachment_slot_ids=(
                    ["slot_file_1"] if attachment else []
                ),
            ))
        return ClientInputEnvelope(
            idempotency_key=key,
            client_type=ClientType.TELEGRAM,
            client_instance_id="bot-1",
            conversation=ClientConversationRef(conversation_id="chat-1"),
            sender=ClientSenderRef(
                principal_id="user-1",
                display_name="User",
            ),
            source_update_id="update-1",
            source_message_id="message-1",
            occurred_at=datetime.now(timezone.utc),
            text_parts=text_parts,
            attachment_slots=slots,
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="chat-1",
                reply_to_message_id="message-1",
            ),
        )

    async def test_streaming_file_commits_exact_input_artifact(self):
        result = await self.ingress.ingress_service.submit_atomic(
            self._envelope(),
            session_id="session-1",
            upload_streams={
                "slot_file_1": chunks(b"hello ", b"world"),
            },
        )

        self.assertEqual(result.state, "committed")
        self.assertIsNotNone(result.committed_batch)
        batch = result.committed_batch
        self.assertEqual(len(batch.artifact_refs), 1)
        artifact_id = batch.artifact_refs[0]
        version = await self.artifacts.artifact_store.get_version(artifact_id)
        lineage = await self.artifacts.artifact_store.get_lineage(
            version.artifact_lineage_id
        )
        self.assertEqual(lineage.session_id, "session-1")
        self.assertEqual(lineage.purpose.value, "input")
        self.assertEqual(version.provenance.origin, "user_upload")
        self.assertEqual(version.provenance.creator, "user")
        self.assertEqual(
            version.provenance.input_batch_id,
            batch.input_batch_id,
        )
        self.assertEqual(
            await self.storage.content_store.read_content(version.content_id),
            b"hello world",
        )
        draft = await self.ingress.batch_store.get_draft(batch.input_batch_id)
        self.assertEqual(draft.state, InputBatchDraftState.COMMITTED)

    async def test_text_only_batch_commits_without_artifacts(self):
        result = await self.ingress.ingress_service.submit_atomic(
            self._envelope(text="Hello", attachment=False),
            session_id="session-1",
        )

        self.assertEqual(result.state, "committed")
        self.assertEqual(result.committed_batch.artifact_refs, [])
        self.assertEqual(result.committed_batch.text_parts[0].text, "Hello")

    async def test_replay_returns_same_event_batch_and_artifact(self):
        envelope = self._envelope()
        first = await self.ingress.ingress_service.submit_atomic(
            envelope,
            session_id="session-1",
            upload_streams={"slot_file_1": chunks(b"hello world")},
        )
        second = await self.ingress.ingress_service.submit_atomic(
            envelope,
            session_id="session-1",
        )

        self.assertTrue(second.duplicate)
        self.assertEqual(second.event_id, first.event_id)
        self.assertEqual(second.input_batch_id, first.input_batch_id)
        self.assertEqual(
            second.committed_batch.artifact_refs,
            first.committed_batch.artifact_refs,
        )
        lineages = await self.artifacts.artifact_store.list_lineages(
            session_id="session-1",
            include_archived=True,
        )
        self.assertEqual(len(lineages), 1)

    async def test_same_idempotency_key_with_different_input_is_rejected(self):
        first = self._envelope(text="One")
        await self.ingress.ingress_service.submit_atomic(
            first,
            session_id="session-1",
            upload_streams={"slot_file_1": chunks(b"hello world")},
        )
        conflicting = self._envelope(text="Two")

        with self.assertRaises(IngressConflictError):
            await self.ingress.ingress_service.submit_atomic(
                conflicting,
                session_id="session-1",
                upload_streams={"slot_file_1": chunks(b"hello world")},
            )

    async def test_missing_stream_fails_without_committed_manifest(self):
        result = await self.ingress.ingress_service.submit_atomic(
            self._envelope(),
            session_id="session-1",
            upload_streams={},
        )

        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, "missing_upload_stream")
        draft = await self.ingress.batch_store.get_draft(result.input_batch_id)
        self.assertEqual(draft.state, InputBatchDraftState.FAILED)
        committed_path = (
            Path(self.storage_config.root_dir)
            / "input_batches"
            / result.input_batch_id
            / "committed.json"
        )
        self.assertFalse(committed_path.exists())

    async def test_filename_is_sanitized_by_artifact_domain(self):
        result = await self.ingress.ingress_service.submit_atomic(
            self._envelope(filename="../../report.md"),
            session_id="session-1",
            upload_streams={"slot_file_1": chunks(b"hello world")},
        )
        version = await self.artifacts.artifact_store.get_version(
            result.committed_batch.artifact_refs[0]
        )
        self.assertEqual(version.filename, "report.md")


if __name__ == "__main__":
    unittest.main()
