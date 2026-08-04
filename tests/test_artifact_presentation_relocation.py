import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from src.interaction.anchors import (
    ClientResponseAnchor,
    ClientResponseAnchorKind,
)
from src.interaction.errors import PresentationConflictError
from src.interaction.presentation import (
    PresentationAckPolicy,
    PresentationDeletionState,
)
from src.interaction.presentation_service import InputPresentationCoordinator
from src.interaction.presentation_store import FileSystemInputPresentationStore
from src.localization.models import LocalizationMessage


class InputPresentationRelocationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = FileSystemInputPresentationStore(self.root)
        self.batch_id = "ibat_" + "1" * 32
        self.binding_id = "telegram:default:12345:-"

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def _anchor(message_id: str, suffix: str) -> ClientResponseAnchor:
        now = datetime.now(timezone.utc)
        return ClientResponseAnchor(
            anchor_id="anch_" + suffix * 32,
            client_message_id=message_id,
            source_event_id=None,
            source_message_id=message_id,
            kind=ClientResponseAnchorKind.INSTRUCTION,
            priority=400,
            occurred_at=now,
            selected_at=now,
            metadata={},
        )

    @staticmethod
    def _message() -> LocalizationMessage:
        return LocalizationMessage(
            message_key="input_batch.collecting",
            params={"file_count": 1, "text_part_count": 1},
        )

    async def _bound_record(self):
        record, created = await self.store.reserve(
            input_batch_id=self.batch_id,
            client_binding_id=self.binding_id,
            token="initial-token",
            message=self._message(),
            locale="ru",
            file_count=1,
            text_part_count=0,
            response_anchor=self._anchor("50", "a"),
        )
        self.assertTrue(created)
        return await self.store.bind(
            record.presentation_id,
            client_message_id="100",
            token="initial-token",
        )

    async def test_relocation_supersedes_old_generation_after_durable_bind(self):
        initial = await self._bound_record()
        reserved = await self.store.reserve_relocation(
            initial.presentation_id,
            token="relocation-token",
            expected_generation=1,
            message=self._message(),
            file_count=2,
            text_part_count=1,
            response_anchor=self._anchor("110", "b"),
        )
        self.assertEqual(reserved.pending_relocation_generation, 2)
        self.assertEqual(reserved.client_message_id, "100")

        relocated = await self.store.bind_relocation(
            initial.presentation_id,
            client_message_id="120",
            token="relocation-token",
            expected_generation=1,
        )
        self.assertEqual(relocated.presentation_generation, 2)
        self.assertEqual(relocated.active_client_message_id, "120")
        self.assertEqual(relocated.anchor_source_message_id, "110")
        self.assertEqual(len(relocated.superseded_handles), 1)
        old = relocated.superseded_handles[0]
        self.assertEqual(old.client_message_id, "100")
        self.assertEqual(old.generation, 1)
        self.assertEqual(
            old.deletion_state,
            PresentationDeletionState.NOT_REQUESTED,
        )

        receipt = await self.store.record_superseded_deletion(
            initial.presentation_id,
            generation=1,
            state=PresentationDeletionState.FAILED,
            token="relocation-token",
        )
        self.assertEqual(
            receipt.superseded_handles[0].deletion_state,
            PresentationDeletionState.FAILED,
        )
        self.assertEqual(receipt.client_message_id, "120")

    async def test_stale_generation_cannot_overwrite_new_active_handle(self):
        initial = await self._bound_record()
        await self.store.reserve_relocation(
            initial.presentation_id,
            token="relocation-token",
            expected_generation=1,
            message=self._message(),
            file_count=2,
            text_part_count=1,
            response_anchor=self._anchor("110", "b"),
        )
        await self.store.bind_relocation(
            initial.presentation_id,
            client_message_id="120",
            token="relocation-token",
            expected_generation=1,
        )

        with self.assertRaises(PresentationConflictError):
            await self.store.bind_relocation(
                initial.presentation_id,
                client_message_id="130",
                token="another-token",
                expected_generation=1,
            )
        current = await self.store.get(initial.presentation_id)
        self.assertEqual(current.client_message_id, "120")
        self.assertEqual(current.presentation_generation, 2)

    async def test_schema_v1_bound_record_is_upgraded_on_read(self):
        initial = await self._bound_record()
        path = self.store.records / f"{initial.presentation_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["schema_version"] = 1
        for field in (
            "presentation_generation",
            "anchor_source_message_id",
            "superseded_handles",
            "pending_relocation_token_hash",
            "pending_relocation_generation",
            "pending_anchor_source_message_id",
        ):
            payload.pop(field, None)
        path.write_text(json.dumps(payload), encoding="utf-8")

        upgraded = await self.store.get(initial.presentation_id)
        self.assertEqual(upgraded.schema_version, 2)
        self.assertEqual(upgraded.presentation_generation, 1)
        self.assertEqual(upgraded.client_message_id, "100")
        self.assertEqual(upgraded.superseded_handles, [])

    async def test_coordinator_reserves_relocation_only_for_later_telegram_anchor(self):
        initial = await self._bound_record()
        coordinator = InputPresentationCoordinator(self.store)

        policy, _, ref = await coordinator.present(
            input_batch_id=self.batch_id,
            client_binding_id=self.binding_id,
            locale="ru",
            state="collecting",
            file_count=2,
            text_part_count=1,
            response_anchor=self._anchor("110", "b"),
        )
        self.assertEqual(policy, PresentationAckPolicy.RELOCATE)
        self.assertEqual(ref.presentation_generation, 1)
        self.assertEqual(ref.relocation_generation, 2)
        self.assertEqual(ref.previous_client_message_id, "100")
        self.assertIsNotNone(ref.presentation_token)

        # A second event while the transport owns the pending create/bind step
        # cannot create another generation or edit the old handle.
        policy2, _, ref2 = await coordinator.present(
            input_batch_id=self.batch_id,
            client_binding_id=self.binding_id,
            locale="ru",
            state="collecting",
            file_count=3,
            text_part_count=1,
            response_anchor=self._anchor("111", "c"),
        )
        self.assertEqual(policy2, PresentationAckPolicy.SILENT)
        self.assertEqual(ref2.relocation_generation, 2)

        self.assertEqual(initial.presentation_generation, 1)

    async def test_non_telegram_binding_does_not_relocate_by_numeric_guess(self):
        record, _ = await self.store.reserve(
            input_batch_id="ibat_" + "2" * 32,
            client_binding_id="web:default:session",
            token="web-token",
            message=self._message(),
            locale="ru",
            response_anchor=self._anchor("50", "a"),
        )
        await self.store.bind(
            record.presentation_id,
            client_message_id="100",
            token="web-token",
        )
        coordinator = InputPresentationCoordinator(self.store)
        policy, _, _ = await coordinator.present(
            input_batch_id="ibat_" + "2" * 32,
            client_binding_id="web:default:session",
            locale="ru",
            state="collecting",
            file_count=0,
            text_part_count=2,
            response_anchor=self._anchor("110", "b"),
        )
        self.assertNotEqual(policy, PresentationAckPolicy.RELOCATE)

    async def test_explicit_collection_can_keep_one_bound_status_message(self):
        await self._bound_record()
        coordinator = InputPresentationCoordinator(self.store)

        policy, _, ref = await coordinator.present(
            input_batch_id=self.batch_id,
            client_binding_id=self.binding_id,
            locale="ru",
            state="collecting",
            file_count=7,
            text_part_count=2,
            response_anchor=self._anchor("110", "c"),
            allow_relocation=False,
        )

        self.assertNotEqual(policy, PresentationAckPolicy.RELOCATE)
        self.assertEqual(ref.client_message_id, "100")
        self.assertIsNone(ref.relocation_generation)


if __name__ == "__main__":
    unittest.main()
