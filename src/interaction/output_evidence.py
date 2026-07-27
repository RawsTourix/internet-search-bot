"""Strict transport-evidence validation before aggregate state mutations."""

from __future__ import annotations

from .errors import OutputBatchConflictError
from .output_completion import OutputDeliveryCompletionService
from .output_models import (
    ArtifactContentReceiptState,
    ArtifactOutputPart,
    OutputDeliveryReceipt,
    OutputPartReceiptState,
)


def validate_external_output_evidence(batch, receipt: OutputDeliveryReceipt) -> None:
    """Reject contradictory transport facts before any durable mutation."""

    part_by_id = {part.part_id: part for part in batch.parts}
    for item in receipt.part_receipts:
        part = part_by_id.get(item.part_id)
        if part is None:
            raise OutputBatchConflictError(
                "delivery evidence references an unknown output part"
            )
        if not isinstance(part, ArtifactOutputPart):
            if item.artifact_content_state is not None:
                raise OutputBatchConflictError(
                    "non-artifact output cannot declare artifact content evidence"
                )
            continue

        content_state = item.artifact_content_state
        if content_state is None:
            # Backward-compatible persisted receipts are inferred by the
            # completion service. New transport executors always emit the field.
            continue
        if content_state == ArtifactContentReceiptState.DELIVERED:
            # A native media/document send can be confirmed before a separate
            # overflow caption becomes ambiguous. In that case the OutputPart is
            # UNKNOWN, while exact artifact bytes are still DELIVERED.
            if item.state not in {
                OutputPartReceiptState.DELIVERED,
                OutputPartReceiptState.PARTIALLY_DELIVERED,
                OutputPartReceiptState.UNKNOWN,
            }:
                raise OutputBatchConflictError(
                    "delivered artifact bytes contradict the part outcome"
                )
            if not item.client_message_ids:
                raise OutputBatchConflictError(
                    "delivered artifact bytes require exact client message IDs"
                )
            if (
                item.state
                in {
                    OutputPartReceiptState.DELIVERED,
                    OutputPartReceiptState.PARTIALLY_DELIVERED,
                }
                and item.delivered_at is None
            ):
                raise OutputBatchConflictError(
                    "confirmed delivered part requires delivered_at"
                )
        elif content_state == ArtifactContentReceiptState.UNKNOWN:
            if item.state != OutputPartReceiptState.UNKNOWN:
                raise OutputBatchConflictError(
                    "unknown artifact bytes require an unknown part outcome"
                )


class ValidatingOutputDeliveryCompletionService(
    OutputDeliveryCompletionService
):
    """Optional composition wrapper for non-HTTP transport integrations."""

    async def complete(self, receipt: OutputDeliveryReceipt):
        batch = await self.output_store.get(receipt.output_batch_id)
        validate_external_output_evidence(batch, receipt)
        return await super().complete(receipt)

    async def reconcile_unknown(self, receipt: OutputDeliveryReceipt):
        batch = await self.output_store.get(receipt.output_batch_id)
        validate_external_output_evidence(batch, receipt)
        return await super().reconcile_unknown(receipt)

    @staticmethod
    def _validate_artifact_authority(batch, part, record) -> None:
        OutputDeliveryCompletionService._validate_artifact_authority(
            batch,
            part,
            record,
        )
        if (
            record.filename != part.filename
            or record.mime_type != part.mime_type
            or record.size_bytes != part.size_bytes
        ):
            raise OutputBatchConflictError(
                "artifact delivery metadata differs from immutable OutputBatch"
            )
