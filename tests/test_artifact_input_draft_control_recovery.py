import tempfile
import unittest
from pathlib import Path

from src.artifacts import ArtifactConfigType, create_artifact_services
from src.core.models import ClientType
from src.ingress import (
    ClientConversationRef,
    ClientResponseRoute,
    IngressConfigType,
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
        )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    def _create_services(self):
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
        return create_ingress_services(
            storage_config=self.storage_config,
            ingress_config=self.ingress_config,
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
