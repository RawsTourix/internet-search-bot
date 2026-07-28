"""Telegram plan executor with exact client-instance content authority."""

from __future__ import annotations

import logging
from typing import Any, Mapping

from ...interaction.output_models import (
    ArtifactOutputPart,
    OutputBatch,
    OutputDeliveryGroup,
    OutputDeliveryPlan,
    OutputDeliveryReceipt,
    OutputPart,
    OutputPartReceipt,
    OutputPartReceiptState,
    TransportOperationKind,
)
from .output_batch_gateway import TelegramClaimedOutputGateway
from .output_context import (
    build_preflight_failure_receipt,
    build_telegram_execution_context,
)
from .output_plan_executor import (
    TelegramExecutionContext,
    TelegramOutputPlanExecutor,
)


logger = logging.getLogger("TelegramServer.OutputExecutor")


class InstanceScopedTelegramOutputPlanExecutor(TelegramOutputPlanExecutor):
    """Execute one claimed immutable batch through exact durable authority."""

    async def execute(
        self,
        *,
        batch: OutputBatch,
        plan: OutputDeliveryPlan,
        attempt_id: str,
        context: TelegramExecutionContext,
    ) -> OutputDeliveryReceipt:
        gateway = context.gateway
        configured_instance = str(
            getattr(gateway, "client_instance_id", "") or ""
        ).strip()
        if (
            configured_instance
            and configured_instance
            != batch.capability_snapshot.client_instance_id
        ):
            raise ValueError(
                "Telegram executor gateway instance differs from OutputBatch"
            )
        if (
            not isinstance(gateway, TelegramClaimedOutputGateway)
            and all(
                hasattr(gateway, attribute)
                for attribute in ("gateway_url", "api_key")
            )
        ):
            gateway = TelegramClaimedOutputGateway.from_client(
                gateway,
                output_batch_id=batch.output_batch_id,
                client_instance_id=(
                    batch.capability_snapshot.client_instance_id
                ),
            )
        try:
            authoritative_context = build_telegram_execution_context(
                batch,
                bot=context.bot,
                gateway=gateway,
                status_message_id=context.status_message_id,
            )
        except (TypeError, ValueError) as error:
            receipt = build_preflight_failure_receipt(
                batch,
                attempt_id=attempt_id,
                error_category=(
                    "telegram_invalid_response_route:"
                    f"{type(error).__name__}"
                ),
            )
            self._log_receipt(batch, receipt)
            return receipt

        receipt = await super().execute(
            batch=batch,
            plan=plan,
            attempt_id=attempt_id,
            context=authoritative_context,
        )
        self._log_receipt(batch, receipt)
        return receipt

    async def _execute_group(
        self,
        *,
        group: OutputDeliveryGroup,
        parts: list[OutputPart],
        context: TelegramExecutionContext,
        reply_to_message_id: int | None,
        limits: Mapping[str, Any],
    ) -> list[OutputPartReceipt]:
        receipts = await super()._execute_group(
            group=group,
            parts=parts,
            context=context,
            reply_to_message_id=reply_to_message_id,
            limits=limits,
        )
        if not self._can_fallback_document_group(
            group=group,
            parts=parts,
            receipts=receipts,
        ):
            return receipts

        logger.warning(
            "telegram_document_group_fallback group_id=%s part_count=%s "
            "categories=%s",
            group.group_id,
            len(parts),
            [item.error_category for item in receipts],
        )
        fallback_receipts: list[OutputPartReceipt] = []
        reply_available = reply_to_message_id
        for part in parts:
            receipt = await self._send_artifact(
                part=part,
                operation=TransportOperationKind.DOCUMENT,
                context=context,
                reply_to_message_id=reply_available,
                limits=limits,
            )
            fallback_receipts.append(receipt)
            if (
                reply_available is not None
                and self._transport_attempted([receipt])
            ):
                reply_available = None

        logger.info(
            "telegram_document_group_fallback_finished group_id=%s "
            "part_states=%s",
            group.group_id,
            [item.state.value for item in fallback_receipts],
        )
        return fallback_receipts

    @staticmethod
    def _can_fallback_document_group(
        *,
        group: OutputDeliveryGroup,
        parts: list[OutputPart],
        receipts: list[OutputPartReceipt],
    ) -> bool:
        """Retry individually only when the grouped attempt is known unsent.

        A confirmed Telegram ``BadRequest`` has no successful transport side
        effect, and any exception raised before ``send_media_group`` starts is
        represented by FAILED receipts. UNKNOWN/PARTIALLY_DELIVERED outcomes are
        never retried because they may already exist in the client.
        """

        if group.operation_kind != TransportOperationKind.DOCUMENT_GROUP:
            return False
        if not 2 <= len(parts) <= 10:
            return False
        if not all(isinstance(part, ArtifactOutputPart) for part in parts):
            return False
        if len(receipts) != len(parts) or not receipts:
            return False
        if any(
            receipt.state != OutputPartReceiptState.FAILED
            for receipt in receipts
        ):
            return False
        safe_prefixes = (
            "telegram_bad_request:",
            "telegram_group_error:",
        )
        return all(
            bool(receipt.error_category)
            and receipt.error_category.startswith(safe_prefixes)
            for receipt in receipts
        )

    @staticmethod
    def _log_receipt(
        batch: OutputBatch,
        receipt: OutputDeliveryReceipt,
    ) -> None:
        logger.info(
            "telegram_output_receipt output_batch_id=%s attempt_id=%s "
            "state=%s parts=%s",
            batch.output_batch_id,
            receipt.attempt_id,
            receipt.state.value,
            [
                {
                    "part_id": item.part_id,
                    "index": item.index,
                    "state": item.state.value,
                    "delivery_id": item.delivery_id,
                    "artifact_content_state": (
                        item.artifact_content_state.value
                        if item.artifact_content_state is not None
                        else None
                    ),
                    "error_category": item.error_category,
                    "client_message_ids": list(item.client_message_ids),
                }
                for item in receipt.part_receipts
            ],
        )
