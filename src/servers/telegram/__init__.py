"""Telegram transport package-wide safety composition.

The low-level webhook module remains import-compatible, but every Telegram
control client uses exact instance-scoped claim/receipt routes, every
``TelegramOutputPlanExecutor`` uses immutable OutputBatch-scoped artifact byte
access, semantic-only Telegram events never enter attachment commit logic, and
IR-6 durable AgentEmission records are delivered beside (not through) the final
OutputBatch outbox.
"""

from __future__ import annotations

from dataclasses import replace

from .emission_outbox import install_on_ready_worker
from .input_handler_policy import install_attachment_handler_registration_policy
from .output_batch_gateway import TelegramClaimedOutputGateway
from .output_control_policy import install_output_control_policy
from .output_plan_executor import TelegramOutputPlanExecutor
from .ready_outbox import TelegramReadyOutboxWorker


install_attachment_handler_registration_policy()
install_output_control_policy()
install_on_ready_worker(TelegramReadyOutboxWorker)
_original_execute = TelegramOutputPlanExecutor.execute


async def _execute_with_scoped_artifact_bytes(
    self,
    *,
    batch,
    plan,
    attempt_id,
    context,
):
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
    return await _original_execute(
        self,
        batch=batch,
        plan=plan,
        attempt_id=attempt_id,
        context=context,
    )


TelegramOutputPlanExecutor.execute = _execute_with_scoped_artifact_bytes


__all__ = ["TelegramOutputPlanExecutor"]
