import unittest
from datetime import datetime, timezone

from src.ingress.models import ClientResponseRoute
from src.interaction.capabilities import (
    build_default_capability_registry,
    build_telegram_capability_declaration,
)
from src.interaction.errors import OutputBatchConflictError
from src.interaction.ids import (
    new_output_attempt_id,
    new_output_part_id,
)
from src.interaction.output_evidence import validate_external_output_evidence
from src.interaction.output_models import (
    ArtifactContentReceiptState,
    ArtifactOutputPart,
    OutputBatchKind,
    OutputDeliveryReceipt,
    OutputDeliveryReceiptState,
    OutputPartReceipt,
    OutputPartReceiptState,
)
from src.interaction.output_store import build_ready_output_batch
from src.artifacts import new_artifact_delivery_id, new_artifact_id


UTC = timezone.utc


class OutputEvidencePolicyTests(unittest.TestCase):
    def setUp(self):
        snapshot = build_default_capability_registry().resolve(
            build_telegram_capability_declaration(),
            client_type="telegram",
            client_instance_id="bot-1",
        )
        self.part = ArtifactOutputPart(
            part_id=new_output_part_id(),
            index=0,
            artifact_id=new_artifact_id(),
            delivery_id=new_artifact_delivery_id(),
            filename="result.bin",
            mime_type="application/octet-stream",
            size_bytes=10,
        )
        self.batch = build_ready_output_batch(
            session_id="session-1",
            cycle_id="cycle-1",
            sequence_number=1,
            kind=OutputBatchKind.FINAL,
            response_route=ClientResponseRoute(
                route_type="telegram",
                conversation_id="100",
            ),
            locale="en",
            capability_snapshot=snapshot,
            parts=(self.part,),
        )
        self.now = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)

    def test_failed_part_cannot_claim_delivered_artifact_bytes(self):
        receipt = self._receipt(
            aggregate=OutputDeliveryReceiptState.FAILED,
            part_state=OutputPartReceiptState.FAILED,
            content_state=ArtifactContentReceiptState.DELIVERED,
            error="transport_failed",
        )
        with self.assertRaises(OutputBatchConflictError):
            validate_external_output_evidence(self.batch, receipt)

    def test_confirmed_failure_cannot_hide_unknown_artifact_bytes(self):
        receipt = self._receipt(
            aggregate=OutputDeliveryReceiptState.FAILED,
            part_state=OutputPartReceiptState.FAILED,
            content_state=ArtifactContentReceiptState.UNKNOWN,
            error="transport_failed",
        )
        with self.assertRaises(OutputBatchConflictError):
            validate_external_output_evidence(self.batch, receipt)

    def test_text_fallback_can_deliver_part_without_artifact_bytes(self):
        receipt = self._receipt(
            aggregate=OutputDeliveryReceiptState.DELIVERED,
            part_state=OutputPartReceiptState.DELIVERED,
            content_state=ArtifactContentReceiptState.NOT_DELIVERED,
            message_ids=("101",),
            delivered_at=self.now,
        )
        validate_external_output_evidence(self.batch, receipt)

    def test_partial_caption_can_preserve_delivered_artifact_bytes(self):
        receipt = self._receipt(
            aggregate=OutputDeliveryReceiptState.PARTIALLY_DELIVERED,
            part_state=OutputPartReceiptState.PARTIALLY_DELIVERED,
            content_state=ArtifactContentReceiptState.DELIVERED,
            message_ids=("101",),
            delivered_at=self.now,
            error="caption_failed",
        )
        validate_external_output_evidence(self.batch, receipt)

    def _receipt(
        self,
        *,
        aggregate,
        part_state,
        content_state,
        message_ids=(),
        delivered_at=None,
        error=None,
    ):
        return OutputDeliveryReceipt(
            output_batch_id=self.batch.output_batch_id,
            attempt_id=new_output_attempt_id(),
            state=aggregate,
            part_receipts=(
                OutputPartReceipt(
                    part_id=self.part.part_id,
                    index=0,
                    required=True,
                    state=part_state,
                    delivery_id=self.part.delivery_id,
                    artifact_content_state=content_state,
                    client_message_ids=message_ids,
                    delivered_at=delivered_at,
                    error_category=error,
                ),
            ),
            started_at=self.now,
            completed_at=self.now,
        )


if __name__ == "__main__":
    unittest.main()
