import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.artifacts import ArtifactConfigType, create_artifact_services
from src.core.models import ClientType
from src.ingress import (
    ClientAttachmentLocator,
    ClientConversationRef,
    ClientInputEnvelope,
    ClientResponseRoute,
    ClientSenderRef,
    IngressAttachmentSlot,
    IngressConfigType,
    InputBatchDraftState,
    InputGroupingMode,
    create_ingress_services,
)
from src.ingress.startup_recovery import reconcile_ingress_after_restart
from src.storage import StorageConfigType, create_storage_services


async def chunks(value: bytes):
    yield value


class ArtifactIngressStartupRecoveryTests(unittest.IsolatedAsyncioTestCase):
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
                media_group_quiet_timeout_seconds=0.0,
                media_group_sealing_grace_seconds=0.0,
                media_group_maximum_wait_seconds=10.0,
            ),
            content_store=storage.content_store,
            artifact_services=self.artifacts,
        )
        self.service = self.ingress.ingress_service
        self.session_id = "telegram:conversation:startup-recovery"

    async def asyncTearDown(self):
        self.temporary.cleanup()

    def _file_envelope(self, suffix: str) -> ClientInputEnvelope:
        return ClientInputEnvelope(
            idempotency_key=f"telegram:startup:file-{suffix}",
            client_type=ClientType.TELEGRAM,
            client_instance_id="default",
            conversation=ClientConversationRef(
                conversation_id="startup-recovery"
            ),
            sender=ClientSenderRef(principal_id="user-1"),
            source_update_id=f"update-{suffix}",
            source_message_id=f"message-{suffix}",
            source_group_id=f"album-{suffix}",
            occurred_at=datetime.now(timezone.utc),
            attachment_slots=[
                IngressAttachmentSlot(
                    slot_id=f"slot-{suffix}",
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
                conversation_id="startup-recovery",
                reply_to_message_id=f"message-{suffix}",
            ),
        )

    async def test_ready_draft_is_committed_before_abandonment(self):
        envelope = self._file_envelope("ready")
        submission = await self.service.submit_atomic(
            envelope,
            session_id=self.session_id,
            upload_streams={"slot-ready": chunks(b"hello")},
            grouping_mode=InputGroupingMode.MEDIA_GROUP,
            grouping_key="ready-group",
        )
        self.assertEqual(submission.state, "collecting")

        report = await reconcile_ingress_after_restart(
            self.service,
            self.ingress.batch_store,
        )

        self.assertEqual(report.committed_count, 1)
        self.assertEqual(report.abandoned_count, 0)
        self.assertEqual(
            report.committed_input_batch_ids,
            (submission.input_batch_id,),
        )
        committed = await self.ingress.batch_store.get_committed(
            submission.input_batch_id
        )
        self.assertEqual(len(committed.artifact_refs), 1)
        self.assertEqual(
            await self.ingress.batch_store.list_open_drafts(
                session_id=self.session_id
            ),
            [],
        )

    async def test_incomplete_draft_is_abandoned_and_removed_from_grouping(self):
        envelope = self._file_envelope("incomplete")
        capability_snapshot, resolved_locale = await self.service._resolve_interaction(
            envelope
        )
        event, _ = await self.ingress.event_store.save_if_absent(
            envelope,
            capability_snapshot=capability_snapshot,
            resolved_locale=resolved_locale,
        )
        draft, _ = await self.ingress.batch_store.create_for_event(
            event,
            session_id=self.session_id,
            grouping_mode=InputGroupingMode.MEDIA_GROUP,
            grouping_key="incomplete-group",
        )
        self.assertEqual(
            len(
                await self.ingress.batch_store.list_open_drafts(
                    session_id=self.session_id
                )
            ),
            1,
        )

        report = await reconcile_ingress_after_restart(
            self.service,
            self.ingress.batch_store,
        )

        self.assertEqual(report.committed_count, 0)
        self.assertEqual(report.abandoned_count, 1)
        self.assertEqual(
            report.abandoned_input_batch_ids,
            (draft.input_batch_id,),
        )
        abandoned = await self.ingress.batch_store.get_draft(
            draft.input_batch_id
        )
        self.assertEqual(abandoned.state, InputBatchDraftState.ABANDONED)
        self.assertEqual(
            abandoned.failure_code,
            "process_restart_abandoned",
        )
        self.assertEqual(
            await self.ingress.batch_store.list_open_drafts(
                session_id=self.session_id
            ),
            [],
        )
        self.assertEqual(
            list(self.ingress.batch_store.group_index_dir.glob("*.json")),
            [],
        )


if __name__ == "__main__":
    unittest.main()
