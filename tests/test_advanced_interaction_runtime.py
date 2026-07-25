import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import ValidationError

from src.agent.protocol import ProgressEvent
from src.artifacts.models import (
    new_artifact_delivery_id,
    new_artifact_id,
)
from src.artifacts.candidate_tools import ArtifactCreateFromContentInput
from src.artifacts.tools import ArtifactCreateTextInput
from src.artifacts import (
    ArtifactAccessContext,
    ArtifactConfigType,
    ArtifactProvenance,
    ArtifactPurpose,
    create_artifact_services,
)
from src.core.models import AgentResult, AgentStatus, ClientType
from src.ingress.models import (
    ClientResponseRoute,
    CommittedInputBatch,
    InputAttachmentPart,
    InputBatchDraft,
    new_ingress_event_id,
    new_input_batch_id,
)
from src.ingress.upgrades import (
    upgrade_committed_input_batch,
    upgrade_input_batch_draft,
)
from src.interaction.anchors import (
    ClientResponseAnchorCandidate,
    ClientResponseAnchorKind,
    ResponseAnchorSelector,
)
from src.interaction.capabilities import (
    ClientCapabilityDeclaration,
    build_default_capability_registry,
    build_telegram_capability_declaration,
)
from src.interaction.config import (
    ClientCapabilitiesConfig,
    LocalizationConfigType,
    OutputRuntimeConfig,
)
from src.interaction.errors import (
    CapabilityValidationError,
    OutputBatchConflictError,
)
from src.interaction.ids import new_output_part_id
from src.interaction.output_models import (
    ArtifactOutputPart,
    LocationOutputPart,
    OutputBatchKind,
    OutputBatchState,
    OutputDeliveryReceipt,
    OutputDeliveryReceiptState,
    OutputPartReceipt,
    OutputPartReceiptState,
    TextOutputPart,
    TransportOperationKind,
)
from src.interaction.output_store import (
    FileSystemOutputBatchStore,
    build_ready_output_batch,
)
from src.interaction.output_service import OutputBatchAssembler
from src.interaction.presentation_store import (
    FileSystemInputPresentationStore,
)
from src.interaction.rendering import CapabilityOutputRenderer
from src.localization.models import LocalizationMessage
from src.localization.service import LocalizationService
from src.storage.config import StorageConfigType
from src.storage import create_storage_services
from src.storage.models import new_content_id
from src.interaction.capability_store import (
    FileSystemCapabilitySnapshotStore,
)


UTC = timezone.utc


class AdvancedInteractionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.registry = build_default_capability_registry()
        self.snapshot = self.registry.resolve(
            build_telegram_capability_declaration(),
            client_type="telegram",
            client_instance_id="bot-1",
        )
        self.route = ClientResponseRoute(
            route_type="telegram",
            conversation_id="chat-1",
        )

    async def asyncTearDown(self):
        self.temporary.cleanup()

    async def test_capabilities_are_strict_immutable_and_deduplicated(self):
        with self.assertRaises(ValidationError):
            ClientCapabilityDeclaration(
                capability_contract_version=1,
                features=("output.text", "output.text"),
            )
        with self.assertRaises(CapabilityValidationError):
            self.registry.resolve(
                ClientCapabilityDeclaration(
                    capability_contract_version=1,
                    features=("output.unregistered",),
                ),
                client_type="web",
                client_instance_id="web-1",
            )

        with self.assertRaises(TypeError):
            self.snapshot.limits[
                "transport.telegram.output.text.max_chars"
            ] = 1
        with self.assertRaises(ValidationError):
            self.snapshot.features = ()

        store = FileSystemCapabilitySnapshotStore(
            StorageConfigType(root_dir=str(self.root / "storage")),
            self.registry,
            ClientCapabilitiesConfig(),
        )
        first, first_duplicate = await store.resolve(
            build_telegram_capability_declaration(),
            client_type="telegram",
            client_instance_id="bot-1",
        )
        second, second_duplicate = await store.resolve(
            build_telegram_capability_declaration(),
            client_type="telegram",
            client_instance_id="bot-1",
        )
        self.assertFalse(first_duplicate)
        self.assertTrue(second_duplicate)
        self.assertEqual(first.capability_snapshot_id, second.capability_snapshot_id)

    async def test_ru_en_localization_and_plural_rules(self):
        localization = LocalizationService.from_directory(
            config=LocalizationConfigType()
        )
        message = LocalizationMessage(
            message_key="common.file_count",
            params={"count": 22},
        )
        self.assertEqual(localization.render(message, locale="ru"), "22 файла")
        self.assertEqual(localization.render(message, locale="en"), "22 files")
        self.assertEqual(
            localization.resolve_locale(
                explicit_locale=None,
                binding_locale="en-US",
                transport_locale="ru",
            ),
            "en",
        )

    async def test_llm_create_contract_requires_explicit_artifact_purpose(self):
        self.assertIn(
            "purpose",
            ArtifactCreateTextInput.model_json_schema()["required"],
        )
        self.assertIn(
            "purpose",
            ArtifactCreateFromContentInput.model_json_schema()["required"],
        )
        self.assertEqual(
            ProgressEvent(
                type="result_ready",
                message="The result is ready.",
            ).type,
            "result_ready",
        )

    async def test_anchor_selection_prefers_instruction_over_later_attachment(self):
        now = datetime.now(UTC)
        selector = ResponseAnchorSelector()
        selected = selector.select([
            ClientResponseAnchorCandidate(
                client_message_id="attachment",
                kind=ClientResponseAnchorKind.ATTACHMENT,
                priority=selector.priority_for(
                    ClientResponseAnchorKind.ATTACHMENT
                ),
                occurred_at=now + timedelta(seconds=10),
            ),
            ClientResponseAnchorCandidate(
                client_message_id="instruction",
                kind=ClientResponseAnchorKind.INSTRUCTION,
                priority=selector.priority_for(
                    ClientResponseAnchorKind.INSTRUCTION
                ),
                occurred_at=now,
            ),
        ])
        self.assertEqual(selected.client_message_id, "instruction")
        with self.assertRaises(TypeError):
            selected.metadata["transport"] = "mutated"

    async def test_presentation_store_keeps_one_handle_and_hashes_token(self):
        store = FileSystemInputPresentationStore(self.root)
        values = {
            "input_batch_id": new_input_batch_id(),
            "client_binding_id": "telegram:bot-1:chat-1",
            "message": LocalizationMessage(
                message_key="input_batch.collecting",
                params={"file_count": 1, "text_part_count": 0},
            ),
            "locale": "ru",
            "file_count": 1,
        }
        first, created = await store.reserve(token="first-secret", **values)
        replay, replay_created = await store.reserve(
            token="second-secret",
            **values,
        )
        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(first.presentation_id, replay.presentation_id)
        self.assertTrue(await store.verify_token(first.presentation_id, "first-secret"))
        self.assertFalse(
            await store.verify_token(first.presentation_id, "second-secret")
        )
        stored_text = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (self.root / "input_presentations").rglob("*.json")
        )
        self.assertNotIn("first-secret", stored_text)
        self.assertNotIn("second-secret", stored_text)

    async def test_renderer_groups_documents_by_snapshot_limit(self):
        declaration = build_telegram_capability_declaration()
        declaration = declaration.model_copy(update={
            "limits": {
                **declaration.limits,
                "transport.telegram.output.document_group.max_items": 2,
            }
        })
        snapshot = self.registry.resolve(
            declaration,
            client_type="telegram",
            client_instance_id="bot-1",
        )
        parts = tuple(self._artifact_part(index) for index in range(3))
        batch = build_ready_output_batch(
            session_id="session-1",
            cycle_id="cycle-1",
            sequence_number=1,
            kind=OutputBatchKind.FINAL,
            response_route=self.route,
            locale="ru",
            capability_snapshot=snapshot,
            parts=parts,
        )
        plan = CapabilityOutputRenderer().plan(batch)
        self.assertEqual(
            [group.operation_kind for group in plan.groups],
            [
                TransportOperationKind.DOCUMENT_GROUP,
                TransportOperationKind.DOCUMENT,
            ],
        )
        self.assertEqual([len(group.part_ids) for group in plan.groups], [2, 1])

    async def test_renderer_produces_deterministic_text_fallback(self):
        snapshot = self.registry.resolve(
            ClientCapabilityDeclaration(
                capability_contract_version=1,
                features=("output.text",),
            ),
            client_type="cli",
            client_instance_id="cli-1",
        )
        batch = build_ready_output_batch(
            session_id="session-1",
            cycle_id="cycle-fallback",
            sequence_number=1,
            kind=OutputBatchKind.FINAL,
            response_route=ClientResponseRoute(
                route_type="cli",
                conversation_id="terminal-1",
            ),
            locale="en",
            capability_snapshot=snapshot,
            parts=(
                LocationOutputPart(
                    part_id=new_output_part_id(),
                    index=0,
                    latitude=57.6261,
                    longitude=39.8845,
                    title="Yaroslavl",
                ),
            ),
        )
        group = CapabilityOutputRenderer().plan(batch).groups[0]
        self.assertEqual(group.operation_kind, TransportOperationKind.TEXT)
        self.assertEqual(
            group.rendered_text,
            "Location: Yaroslavl: 57.626100, 39.884500",
        )

    async def test_output_commit_is_semantically_idempotent(self):
        store = FileSystemOutputBatchStore(self.root)
        first = self._text_batch("same result")
        committed, created = await store.commit(first)
        replay, replay_created = await store.commit(
            self._text_batch("same result")
        )
        self.assertTrue(created)
        self.assertFalse(replay_created)
        self.assertEqual(committed.output_batch_id, replay.output_batch_id)

        with self.assertRaises(OutputBatchConflictError):
            await store.commit(self._text_batch("different result"))

    async def test_unknown_receipt_is_terminal_and_never_recoverable(self):
        store = FileSystemOutputBatchStore(self.root)
        batch, _ = await store.commit(self._text_batch("result"))
        claimed, attempt_id = await store.claim_delivery(batch.output_batch_id)
        self.assertEqual(claimed.state, OutputBatchState.DELIVERING)
        with self.assertRaises(OutputBatchConflictError):
            await store.claim_delivery(batch.output_batch_id)

        now = datetime.now(UTC)
        receipt = OutputDeliveryReceipt(
            output_batch_id=batch.output_batch_id,
            attempt_id=attempt_id,
            state=OutputDeliveryReceiptState.UNKNOWN,
            part_receipts=(
                OutputPartReceipt(
                    part_id=batch.parts[0].part_id,
                    index=0,
                    state=OutputPartReceiptState.UNKNOWN,
                    error_category="transport_timeout_after_send",
                ),
            ),
            started_at=now,
            completed_at=now,
        )
        completed = await store.complete(receipt)
        self.assertEqual(completed.state, OutputBatchState.FAILED)
        self.assertEqual(await store.list_recoverable(), [])
        replay = await store.complete(receipt)
        self.assertEqual(replay.state, OutputBatchState.FAILED)

    async def test_receipt_must_cover_committed_parts_in_order(self):
        store = FileSystemOutputBatchStore(self.root)
        batch, _ = await store.commit(self._text_batch("result"))
        _, attempt_id = await store.claim_delivery(batch.output_batch_id)
        now = datetime.now(UTC)
        with self.assertRaises(OutputBatchConflictError):
            await store.complete(OutputDeliveryReceipt(
                output_batch_id=batch.output_batch_id,
                attempt_id=attempt_id,
                state=OutputDeliveryReceiptState.DELIVERED,
                part_receipts=(),
                started_at=now,
                completed_at=now,
            ))

    async def test_stale_delivery_claim_reconciles_to_unknown_without_resend(self):
        store = FileSystemOutputBatchStore(self.root)
        batch, _ = await store.commit(self._text_batch("result"))
        now = datetime.now(UTC)
        await store.claim_delivery(
            batch.output_batch_id,
            now=now - timedelta(seconds=10),
        )
        reconciled = await store.reconcile_stale_claims(
            timeout_seconds=5,
            now=now,
        )
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0].state, OutputBatchState.FAILED)
        self.assertEqual(await store.list_recoverable(), [])

    async def test_final_assembly_keeps_text_then_selected_artifact_order(self):
        storage_config = StorageConfigType(
            root_dir=str(self.root / "assembly-storage")
        )
        storage = create_storage_services(storage_config)
        artifacts = create_artifact_services(
            storage_config=storage_config,
            artifact_config=ArtifactConfigType(
                max_artifact_size_bytes=1024 * 1024,
                max_patchable_text_bytes=1024 * 1024,
                max_workspace_bytes=2 * 1024 * 1024,
            ),
            content_store=storage.content_store,
        )
        access = ArtifactAccessContext(
            session_id="session-1",
            cycle_id="cycle-1",
            allowed_artifact_ids=[],
        )
        created = []
        for filename in ("second.md", "first.md"):
            item = await artifacts.artifact_service.create_text(
                session_id="session-1",
                cycle_id="cycle-1",
                filename=filename,
                text=filename,
                format_id="markdown",
                purpose=ArtifactPurpose.DELIVERABLE,
                provenance=ArtifactProvenance(
                    origin="agent_created",
                    creator="agent",
                    operation="assembly_test",
                ),
            )
            access.allowed_artifact_ids.append(item.artifact_id)
            created.append(item)
        await artifacts.delivery_service.select_many(
            artifact_ids=[item.artifact_id for item in created],
            access=access,
            client_type="telegram",
        )

        output_store = FileSystemOutputBatchStore(
            Path(storage_config.root_dir)
        )
        assembler = OutputBatchAssembler(
            config=OutputRuntimeConfig(),
            delivery_store=artifacts.delivery_store,
            output_store=output_store,
        )
        input_batch = self._committed_batch()
        result = AgentResult(
            content="Final text",
            status=AgentStatus.DONE,
            session_id="session-1",
            cycle_id="cycle-1",
        )
        batch = await assembler.assemble_final(
            result=result,
            input_batch=input_batch,
        )
        self.assertEqual(
            [part.type for part in batch.parts],
            ["text_output", "artifact_output", "artifact_output"],
        )
        self.assertEqual(
            [part.filename for part in batch.parts[1:]],
            ["second.md", "first.md"],
        )
        replay = await assembler.assemble_final(
            result=AgentResult(
                content="Final text",
                status=AgentStatus.DONE,
                session_id="session-1",
                cycle_id="cycle-1",
            ),
            input_batch=input_batch,
        )
        self.assertEqual(batch.output_batch_id, replay.output_batch_id)

    async def test_v1_ingress_records_upgrade_without_inventing_lineage(self):
        now = datetime.now(UTC).isoformat()
        event_id = new_ingress_event_id()
        batch_id = new_input_batch_id()
        artifact_id = new_artifact_id()
        attachment = {
            "slot_id": "slot-old",
            "state": "stored",
            "content_id": new_content_id(),
            "artifact_id": artifact_id,
            "detected_format_id": "text",
            "detected_mime_type": "text/plain",
            "size_bytes": 3,
            "content_hash": "sha256:" + ("a" * 64),
        }
        InputAttachmentPart.model_validate(attachment)
        draft_payload = {
            "schema_version": 1,
            "input_batch_id": batch_id,
            "session_id": "session-1",
            "client_type": "telegram",
            "conversation": {"conversation_id": "chat-1"},
            "sender": {"principal_id": "user-1"},
            "grouping_mode": "atomic",
            "grouping_key": "old-record",
            "state": "ready_to_commit",
            "source_event_ids": [event_id],
            "attachment_parts": [attachment],
            "admission_mode": "auto",
            "response_route": self.route.model_dump(mode="json"),
            "opened_at": now,
            "last_event_at": now,
            "updated_at": now,
        }
        upgraded_draft = upgrade_input_batch_draft(draft_payload)
        self.assertNotIn("artifact_manifest", upgraded_draft)
        InputBatchDraft.model_validate(upgraded_draft)

        committed_payload = {
            "schema_version": 1,
            "input_batch_id": batch_id,
            "session_id": "session-1",
            "client_type": "telegram",
            "sequence_number": 1,
            "source_event_ids": [event_id],
            "artifact_refs": [artifact_id],
            "admission_mode": "auto",
            "response_route": self.route.model_dump(mode="json"),
            "committed_at": now,
            "commit_reason": "legacy",
            "content_fingerprint": "sha256:" + ("b" * 64),
        }
        upgraded_committed = upgrade_committed_input_batch(committed_payload)
        batch = CommittedInputBatch.model_validate(upgraded_committed)
        self.assertEqual(batch.artifact_manifest.available_count, 1)
        self.assertTrue(batch.artifact_manifest.truncated)

    def _text_batch(self, text: str):
        return build_ready_output_batch(
            session_id="session-1",
            cycle_id="cycle-1",
            sequence_number=1,
            kind=OutputBatchKind.FINAL,
            response_route=self.route,
            locale="ru",
            capability_snapshot=self.snapshot,
            parts=(
                TextOutputPart(
                    part_id=new_output_part_id(),
                    index=0,
                    text=text,
                ),
            ),
        )

    def _committed_batch(self) -> CommittedInputBatch:
        now = datetime.now(UTC)
        return CommittedInputBatch(
            input_batch_id=new_input_batch_id(),
            session_id="session-1",
            client_type=ClientType.TELEGRAM,
            sequence_number=1,
            source_event_ids=[new_ingress_event_id()],
            admission_mode="auto",
            response_route=self.route,
            locale="ru",
            capability_snapshot=self.snapshot,
            committed_at=now,
            commit_reason="test",
            content_fingerprint="sha256:" + ("c" * 64),
        )

    @staticmethod
    def _artifact_part(index: int) -> ArtifactOutputPart:
        return ArtifactOutputPart(
            part_id=new_output_part_id(),
            index=index,
            artifact_id=new_artifact_id(),
            delivery_id=new_artifact_delivery_id(),
            filename=f"result-{index}.txt",
            mime_type="text/plain",
            size_bytes=10,
        )


if __name__ == "__main__":
    unittest.main()
