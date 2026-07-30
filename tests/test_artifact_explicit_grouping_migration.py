import json
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
    InputDraftScope,
    InputGroupingMode,
    create_ingress_services,
)
from src.ingress.explicit_policy import EXPLICIT_COLLECTION_GROUPING_MODE
from src.storage import StorageConfigType, create_storage_services


class ExplicitGroupingMigrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.storage_config = StorageConfigType(
            root_dir=str(self.root / "storage")
        )
        self.ingress_config = IngressConfigType(
            max_batch_total_bytes=2 * 1024 * 1024,
            media_group_quiet_timeout_seconds=0.01,
            media_group_sealing_grace_seconds=0.0,
            media_group_maximum_wait_seconds=1.0,
        )
        self.session_id = "telegram:conversation:migration"

    def tearDown(self):
        self.temporary.cleanup()

    def _services(self):
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

    def _scope(self):
        return InputDraftScope(
            session_id=self.session_id,
            client_type=ClientType.TELEGRAM,
            client_instance_id="bot-1",
            conversation=ClientConversationRef(conversation_id="migration"),
            principal_id="user-1",
        )

    @staticmethod
    def _route():
        return ClientResponseRoute(
            route_type="telegram",
            conversation_id="migration",
        )

    def _envelope(self):
        return ClientInputEnvelope(
            idempotency_key="telegram:bot-1:migration:1",
            client_type=ClientType.TELEGRAM,
            client_instance_id="bot-1",
            conversation=ClientConversationRef(conversation_id="migration"),
            sender=ClientSenderRef(principal_id="user-1"),
            source_message_id="1",
            occurred_at=datetime.now(timezone.utc),
            text_parts=[
                IngressTextPart(
                    part_id="migration-text",
                    kind="message_text",
                    text="migrate me",
                )
            ],
            response_route=self._route(),
        )

    async def test_restart_rewrites_rollout_marker_and_group_index(self):
        services = self._services()
        started = await services.draft_control_service.start_collection(
            self._scope(),
            response_route=self._route(),
            locale="ru",
            idempotency_key="collect-migration",
        )
        submitted = await services.ingress_service.submit_atomic(
            self._envelope(),
            session_id=self.session_id,
        )
        draft = await services.batch_store.get_draft(submitted.input_batch_id)
        collection_id = started.collection.collection_id
        self.assertEqual(draft.grouping_mode, EXPLICIT_COLLECTION_GROUPING_MODE)

        draft_path = services.batch_store.root / draft.input_batch_id / "draft.json"
        payload = json.loads(draft_path.read_text(encoding="utf-8"))
        payload["grouping_mode"] = InputGroupingMode.IMMEDIATE_TEXT.value
        draft_path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )

        canonical_index = services.batch_store._group_index_path(
            session_id=draft.session_id,
            grouping_mode=EXPLICIT_COLLECTION_GROUPING_MODE,
            grouping_key=collection_id,
        )
        legacy_index = services.batch_store._group_index_path(
            session_id=draft.session_id,
            grouping_mode=InputGroupingMode.IMMEDIATE_TEXT,
            grouping_key=collection_id,
        )
        canonical_index.unlink()
        legacy_index.parent.mkdir(parents=True, exist_ok=True)
        legacy_index.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "session_id": draft.session_id,
                    "grouping_mode": InputGroupingMode.IMMEDIATE_TEXT.value,
                    "grouping_key": collection_id,
                    "input_batch_id": draft.input_batch_id,
                }
            ),
            encoding="utf-8",
        )

        restarted = self._services()
        await restarted.ingress_service.commit_ready_drafts()

        migrated = await restarted.batch_store.get_draft(draft.input_batch_id)
        self.assertEqual(
            migrated.grouping_mode,
            InputGroupingMode.EXPLICIT_COLLECTION,
        )
        self.assertEqual(migrated.grouping_key, collection_id)
        self.assertFalse(legacy_index.exists())
        self.assertTrue(canonical_index.exists())
        persisted = json.loads(draft_path.read_text(encoding="utf-8"))
        self.assertEqual(
            persisted["grouping_mode"],
            InputGroupingMode.EXPLICIT_COLLECTION.value,
        )


if __name__ == "__main__":
    unittest.main()
