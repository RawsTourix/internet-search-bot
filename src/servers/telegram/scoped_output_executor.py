"""Telegram plan executor with exact client-instance content authority."""

from __future__ import annotations

from ...interaction.output_models import (
    OutputBatch,
    OutputDeliveryPlan,
    OutputDeliveryReceipt,
)
from .output_plan_executor import (
    TelegramExecutionContext,
    TelegramOutputPlanExecutor,
)


class InstanceScopedTelegramOutputPlanExecutor(TelegramOutputPlanExecutor):
    """Bind one claimed immutable batch for the duration of transport execution.

    The binding is an in-memory routing projection used only to select the
    instance-scoped delivery-content endpoint. It neither creates nor owns the
    durable claim, which remains in the Gateway OutputBatch store.
    """

    async def execute(
        self,
        *,
        batch: OutputBatch,
        plan: OutputDeliveryPlan,
        attempt_id: str,
        context: TelegramExecutionContext,
    ) -> OutputDeliveryReceipt:
        bind = getattr(context.gateway, "bind_output_claim", None)
        release = getattr(context.gateway, "release_output_claim", None)
        if bind is not None:
            gateway_instance = str(
                getattr(context.gateway, "client_instance_id", "") or ""
            ).strip()
            if (
                gateway_instance
                and gateway_instance
                != batch.capability_snapshot.client_instance_id
            ):
                raise ValueError(
                    "Telegram executor gateway instance differs from OutputBatch"
                )
            bind(batch)
        try:
            return await super().execute(
                batch=batch,
                plan=plan,
                attempt_id=attempt_id,
                context=context,
            )
        finally:
            if release is not None:
                release(batch.output_batch_id)
