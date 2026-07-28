import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.api.session_reset import reset_runtime_session
from src.artifacts import ArtifactConfigType, create_artifact_services
from src.artifacts.errors import ArtifactStorageError
from src.core.models import ClientType
from src.ingress import (
    ClientAttachmentLocator,
    ClientConversationRef,
    ClientInputEnvelope,
    ClientResponseRoute,
    ClientSenderRef,
    IngressAttachmentSlot,
    IngressConfigType,
    IngressTextPart,
    InputBatchDraftState,
    InputGroupingMode,
    create_ingress_services,
)
from src.ingress.store import IngressConflictError
from src.storage import StorageConfigType, create_storage_services


async def chunks(value: bytes):
    yield value


class _FakeMcpClient:
    def __init__(self):
        self.cleared = []

    def clear_session(self, session_id: str) -> None:
        self.cleared.append(session_id)


class ArtifactIngressFailureRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.storage_config = StorageConfigType(
            root_dir=str(self.root / "storage")
        )
        storage = create_storage_services(self.storage_config)
        self.artifacts = create_artifact_services(
            storage_config=self.storage_config,
            artifact_config=ArtifactConfigType(
                max_artifact_size_bytes=1024 * 1024,
                max_patchable_text_bytes=1024 * 1024,
                max_workspace_bytes=2 * 1024 * 1024,
            ),
            content_store=storage.content_store,
        )
        self.ingress = create_ingress_services(
            storage_config=self.storage_config,
            ingress_config=IngressConfigType(
                max_batch_total_bytes=2 * 1024 * 1024,
                media_group_quiet_timeout_seconds=0.01,
                media_group_sealing_grace_seconds=0.0,
                media_group_maximum_wait_seconds=1.0,
            ),
            content_store=storage.content_store,
            artifact_services=self.artifacts,
        )
        self.service = self.ingress.ingress_service
        self.session_id = "telegram:conversation:chat-recovery"

    async def asyncTearDown(self):
        self.temporary.cleanup()

    def _file_envelope(self, suffix: str = "1") -> ClientInputEnvelope:
        return ClientInputEnvelope(
            idempotency_key=f"telegram:bot-1:update:file-{suffix}",
            client_type=ClientType.TELEGRAM,
            client_instance_id="bot-1",
            conversation=ClientConversationRef(
                conversation_id="chat-recovery"
            ),
            sender=ClientSenderRef(principal_id="user-1"),
            source_update_id=f"file-update-{suffix}",
            source_message_id=f"file-message-{suffix}",
            source_group_id=f"album-{suffix}",
            occurred_at=datetime.now(timezone.utc),
            attachment_slots=[
                IngressAttachmentSlot(
                    slot_id=f"slot-file-{suffix}",
                    media_kind="document",
                    original_filename=f"source-{suffix}.md",
                    declared_mime_type="text/markdown",
                    declared_size_bytes=5,
                    transport_locator=ClientAttachmentLocator(
                        provider="telegram",
                        locator=f"telegram-file-{suffix}",
                    ),
                )
            ],
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="chat-recovery",
                reply_to_message_id=f"file-message-{suffix}",
            ),
        )

    def _instruction_envelope(self, suffix: str = "1") -> ClientInputEnvelope:
        return ClientInputEnvelope(
            idempotency_key=f"telegram:bot-1:update:instruction-{suffix}",
            client_type=ClientType.TELEGRAM,
            client_instance_id="bot-1",
            conversation=ClientConversationRef(
                conversation_id="chat-recovery"
            ),
            sender=ClientSenderRef(principal_id="user-1"),
            source_update_id=f"instruction-update-{suffix}",
            source_message_id=f"instruction-message-{suffix}",
            occurred_at=datetime.now(timezone.utc),
            text_parts=[
                IngressTextPart(
                    part_id=f"text-instruction-{suffix}",
                    kind="message_text",
                    text=f"Process package {suffix}",
                    attachment_slot_ids=[],
                )
            ],
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="chat-recovery",
                reply_to_message_id=f"instruction-message-{suffix}",
            ),
        )

    def _all_drafts(self):
        return sorted(
            self.ingress.batch_store.root.glob("ibat_*/draft.json")
        )

    async def test_storage_failure_closes_draft_and_next_package_can_join(self):
        original_create = self.artifacts.artifact_store.create_lineage
        self.artifacts.artifact_store.create_lineage = AsyncMock(
            side_effect=ArtifactStorageError("simulated publish failure")
        )
        first = self._file_envelope("failed")
        try:
            with self.assertRaises(ArtifactStorageError):
                await self.service.submit_atomic(
                    first,
                    session_id=self.session_id,
                    upload_streams={
                        "slot-file-failed": chunks(b"hello")
                    },
                )
        finally:
            self.artifacts.artifact_store.create_lineage = original_create

        open_drafts = await self.ingress.batch_store.list_open_drafts(
            session_id=self.session_id
        )
        self.assertEqual(open_drafts, [])
        draft_paths = self._all_drafts()
        self.assertEqual(len(draft_paths), 1)
        failed_id = draft_paths[0].parent.name
        failed = await self.ingress.batch_store.get_draft(failed_id)
        self.assertEqual(failed.state, InputBatchDraftState.FAILED)
        self.assertEqual(
            failed.failure_code,
            "artifact_ingress_storage_failed",
        )
        self.assertEqual(
            list(self.ingress.batch_store.group_index_dir.glob("*.json")),
            [],
        )

        second = self._file_envelope("next")
        file_result = await self.service.submit_atomic(
            second,
            session_id=self.session_id,
            upload_streams={"slot-file-next": chunks(b"hello")},
        )
        instruction_result = await self.service.submit_atomic(
            self._instruction_envelope("next"),
            session_id=self.session_id,
        )
        self.assertEqual(file_result.state, "collecting")
        self.assertEqual(instruction_result.state, "collecting")
        self.assertEqual(
            file_result.input_batch_id,
            instruction_result.input_batch_id,
        )
        self.assertNotEqual(file_result.input_batch_id, failed_id)

    async def test_failed_draft_cannot_return_to_collecting(self):
        envelope = self._file_envelope("terminal")
        event = await self.service._persist_event(envelope)
        draft, _ = await self.ingress.batch_store.create_for_event(
            event,
            session_id=self.session_id,
            grouping_mode=InputGroupingMode.MEDIA_GROUP,
            grouping_key="terminal-group",
        )
        failed = await self.ingress.batch_store.fail(
            draft.input_batch_id,
            code="simulated_failure",
            slot_id="slot-file-terminal",
        )
        self.assertEqual(failed.state, InputBatchDraftState.FAILED)
        with self.assertRaises(IngressConflictError):
            await self.ingress.batch_store.mark_collecting(
                draft.input_batch_id
            )

    async def test_session_reset_cancels_open_drafts_and_clears_memory(self):
        envelope = self._file_envelope("reset")
        result = await self.service.submit_atomic(
            envelope,
            session_id=self.session_id,
            upload_streams={"slot-file-reset": chunks(b"hello")},
        )
        self.assertEqual(result.state, "collecting")

        fake_mcp = _FakeMcpClient()
        fake_api = SimpleNamespace(
            ingress_services=self.ingress,
            mcp_client=fake_mcp,
        )
        reset = await reset_runtime_session(fake_api, self.session_id)

        self.assertEqual(reset.cancelled_input_batch_count, 1)
        self.assertEqual(
            reset.cancelled_input_batch_ids,
            (result.input_batch_id,),
        )
        self.assertEqual(fake_mcp.cleared, [self.session_id])
        self.assertEqual(
            await self.ingress.batch_store.list_open_drafts(
                session_id=self.session_id
            ),
            [],
        )
        cancelled = await self.ingress.batch_store.get_draft(
            result.input_batch_id
        )
        self.assertEqual(cancelled.state, InputBatchDraftState.CANCELLED)
        self.assertEqual(cancelled.failure_code, "session_reset")
        self.assertEqual(
            list(self.ingress.batch_store.group_index_dir.glob("*.json")),
            [],
        )


class ArtifactMetadataPublishRetryTests(unittest.TestCase):
    def test_transient_permission_error_retries_only_directory_publish(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            storage_config = StorageConfigType(root_dir=str(root / "storage"))
            storage = create_storage_services(storage_config)
            artifacts = create_artifact_services(
                storage_config=storage_config,
                artifact_config=ArtifactConfigType(),
                content_store=storage.content_store,
            )
            store = artifacts.artifact_store
            store._serialize = lambda model, object_type, object_id: b"{}"
            real_replace = store._replace_directory
            attempts = []

            def flaky_replace(source: Path, target: Path) -> None:
                attempts.append((source, target))
                if len(attempts) == 1:
                    raise PermissionError(13, "transient access denied")
                real_replace(source, target)

            store._replace_directory = flaky_replace
            object_id = "art_" + "0" * 32
            store._write_new_metadata_object(
                parent=store.versions_dir,
                object_type="artifact version",
                object_id=object_id,
                model=object(),
            )

            self.assertEqual(len(attempts), 2)
            self.assertTrue(
                (store.versions_dir / object_id / "metadata.json").is_file()
            )
            self.assertEqual(
                list(store.versions_dir.glob(f".tmp-{object_id}-*")),
                [],
            )


if __name__ == "__main__":
    unittest.main()
