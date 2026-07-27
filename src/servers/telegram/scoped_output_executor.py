"""Telegram plan executor with exact client-instance content authority."""

from __future__ import annotations

from ...interaction.output_models import (
    OutputBatch,
    OutputDeliveryPlan,
    OutputDeliveryReceipt,
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
            return build_preflight_failure_receipt(
                batch,
                attempt_id=attempt_id,
                error_category=(
                    "telegram_invalid_response_route:"
                    f"{type(error).__name__}"
                ),
            )
        return await super().execute(
            batch=batch,
            plan=plan,
            attempt_id=attempt_id,
            context=authoritative_context,
        )
