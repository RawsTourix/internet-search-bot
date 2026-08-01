import asyncio
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
    IngressConfigType,
    IngressTextPart,
    InputBatchDraftState,
    InputDraftControlStatus,
    InputDraftScope,
    create_ingress_services,
)
from src.storage import StorageConfigType, create_storage_services


class InputDraftControlRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "storage"
        self.storage_config = StorageConfigType(root_dir=str(self.root))
        self.ingress_config = IngressConfigType(
            max_batch_total_bytes=2 * 1024 * 1024,
            media_group_quiet_timeout_seconds=0.01,
            media_group_sealing_grace_seconds=0.0,
            media_group_maximum_wait_seconds=1.0,
            explicit_collection_idle_timeout_seconds=60.0,
        )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    def _create_services(self, *, idle_timeout_seconds: float | None = None):
        storage = create_storage_services(self.storage_config)
        artifacts = create_artifact_services(
            storage_config=self.storage_config,
            artifact_config=ArtifactConfigType(
                max_artifact_size_bytes=1024 * 1024,
                max_patchable_text_bytes=1024 * 1024,
                max_workspace_bytes=2 * 1024 * 1024,
            ),
            content_store=storage.content_store,
        )
        config = self.ingress_config
        if idle_timeout_seconds is not None:
            config = config.model_copy(update={
                "explicit_collection_idle_timeout_seconds": idle_timeout_seconds
            })
        return create_ingress_services(
            storage_config=self.storage_config,
            ingress_config=config,
            content_store=storage.content_store,
            artifact_services=artifacts,
        )

    @staticmethod
    def _scope() -> InputDraftScope:
        return InputDraftScope(
            session_id="telegram:conversation:chat-1",
            client_type=ClientType.TELEGRAM,
            client_instance_id="bot-1",
            conversation=ClientConversationRef(conversation_id="chat-1"),
            principal_id="user-1",
        )

    @staticmethod
    def _route() -> ClientResponseRoute:
        return ClientResponseRoute(
            route_type="telegram",
            conversation_id="chat-1",
        )

    @classmethod
    def _text_envelope(cls) -> ClientInputEnvelope:
        return ClientInputEnvelope(
            idempotency_key="telegram:bot-1:update:idle-text",
            client_type=ClientType.TELEGRAM,
            client_instance_id="bot-1",
            conversation=ClientConversationRef(conversation_id="chat-1"),
            sender=ClientSenderRef(principal_id="user-1"),
            source_update_id="idle-update",
            source_message_id="idle-message",
            occurred_at=datetime.now(timezone.utc),
            text_parts=[
                IngressTextPart(
                    part_id="idle-text-part",
                    kind="message_text",
                    text="unfinished package",
                )
            ],
            response_route=cls._route(),
        )

    async def test_active_collection_and_action_result_survive_restart(self):
        first_services = self._create_services()
        first = await first_services.draft_control_service.start_collection(
            self._scope(),
            response_route=self._route(),
            locale="ru",
            idempotency_key="collect-1",
        )

        restarted = self._create_services()
        inspected = await restarted.draft_control_service.inspect(self._scope())
        retry = await restarted.draft_control_service.start_collection(
            self._scope(),
            response_route=self._route(),
            locale="ru",
            idempotency_key="collect-1",
        )

        self.assertEqual(inspected.status, InputDraftControlStatus.INSPECTED)
        self.assertEqual(
            inspected.collection.collection_id,
            first.collection.collection_id,
        )
        self.assertTrue(retry.duplicate)
        self.assertEqual(
            retry.collection.collection_id,
            first.collection.collection_id,
        )

    async def test_idle_collection_and_bound_draft_are_abandoned_after_restart(self):
        first_services = self._create_services(idle_timeout_seconds=0.5)
        first = await first_services.draft_control_service.start_collection(
            self._scope(),
            response_route=self._route(),
            locale="ru",
            idempotency_key="collect-idle-1",
        )
        submission = await first_services.ingress_service.submit_atomic(
            self._text_envelope(),
            session_id=self._scope().session_id,
        )
        self.assertEqual(submission.state, "collecting")

        await asyncio.sleep(0.6)
        restarted = self._create_services(idle_timeout_seconds=0.5)
        await restarted.ingress_service.commit_ready_drafts()

        inspected = await restarted.draft_control_service.inspect(self._scope())
        abandoned_collection = await restarted.collection_store.get(
            first.collection.collection_id
        )
        abandoned_draft = await restarted.batch_store.get_draft(
            submission.input_batch_id
        )
        second = await restarted.draft_control_service.start_collection(
            self._scope(),
            response_route=self._route(),
            locale="ru",
            idempotency_key="collect-idle-2",
        )

        self.assertEqual(inspected.status, InputDraftControlStatus.NOT_FOUND)
        self.assertEqual(abandoned_collection.state.value, "abandoned")
        self.assertEqual(
            abandoned_collection.failure_code,
            "explicit_collection_idle_timeout",
        )
        self.assertEqual(
            abandoned_draft.state,
            InputBatchDraftState.ABANDONED,
        )
        self.assertEqual(second.status, InputDraftControlStatus.STARTED)
        self.assertNotEqual(
            second.collection.collection_id,
            first.collection.collection_id,
        )

    async def test_terminal_bound_draft_closes_collection_during_inspect(self):
        services = self._create_services()
        started = await services.draft_control_service.start_collection(
            self._scope(),
            response_route=self._route(),
            locale="ru",
            idempotency_key="collect-failed",
        )
        submission = await services.ingress_service.submit_atomic(
            self._text_envelope(),
            session_id=self._scope().session_id,
        )
        await services.batch_store.fail(
            submission.input_batch_id,
            code="simulated_terminal_failure",
        )

        inspected = await services.draft_control_service.inspect(self._scope())
        collection = await services.collection_store.get(
            started.collection.collection_id
        )

        self.assertEqual(inspected.status, InputDraftControlStatus.NOT_FOUND)
        self.assertEqual(collection.state.value, "failed")
        self.assertEqual(
            collection.failure_code,
            "simulated_terminal_failure",
        )
        self.assertIsNone(
            await services.collection_store.get_active(self._scope())
        )

    async def test_terminal_collection_releases_scope_for_new_collection(self):
        services = self._create_services()
        first = await services.draft_control_service.start_collection(
            self._scope(),
            response_route=self._route(),
            locale="ru",
            idempotency_key="collect-1",
        )
        cancelled = await services.draft_control_service.cancel(
            self._scope(),
            idempotency_key="cancel-1",
        )
        second = await services.draft_control_service.start_collection(
            self._scope(),
            response_route=self._route(),
            locale="ru",
            idempotency_key="collect-2",
        )

        self.assertEqual(cancelled.status, InputDraftControlStatus.CANCELLED)
        self.assertEqual(second.status, InputDraftControlStatus.STARTED)
        self.assertNotEqual(
            second.collection.collection_id,
            first.collection.collection_id,
        )


if __name__ == "__main__":
    unittest.main()
