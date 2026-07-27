"""Telegram plan executor with exact client-instance content authority."""

from __future__ import annotations

from dataclasses import replace

from ...interaction.output_models import (
    OutputBatch,
    OutputDeliveryPlan,
    OutputDeliveryReceipt,
)
from .output_batch_gateway import TelegramClaimedOutputGateway
from .output_plan_executor import (
    TelegramExecutionContext,
    TelegramOutputPlanExecutor,
)


class InstanceScopedTelegramOutputPlanExecutor(TelegramOutputPlanExecutor):
    """Execute one claimed immutable batch through a narrow byte gateway.

    Each call receives its own immutable OutputBatch-bound facade. No mutable
    process-global claim binding is used, so concurrent chats and future worker
    replicas cannot leak delivery authority into one another.
    """

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
            context = replace(context, gateway=gateway)
        return await super().execute(
            batch=batch,
            plan=plan,
            attempt_id=attempt_id,
            context=context,
        )
