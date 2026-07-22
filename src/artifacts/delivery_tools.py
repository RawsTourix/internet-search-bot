"""Manager tool for explicit artifact delivery selection."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from ..mcp.manager_context import ManagerToolContext
from .delivery import ArtifactDeliveryService
from .errors import (
    ArtifactAccessError,
    ArtifactDeliveryError,
    ArtifactDeliveryNotFoundError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStorageError,
)
from .models import ArtifactAccessContext, ArtifactDeliveryState
from .tools import ArtifactToolDefinition, ArtifactToolOutcome


ARTIFACT_DELIVERY_TOOL_NAMES = frozenset({"artifact_set_delivery"})


class ArtifactSetDeliveryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    selected: bool = True


ARTIFACT_DELIVERY_TOOL_DEFINITIONS = (
    ArtifactToolDefinition(
        name="artifact_set_delivery",
        description=(
            "Выбрать exact immutable artifact version для доставки текущему "
            "клиенту либо отменить ещё не начатую доставку. Инструмент не "
            "передаёт bytes и не управляет Telegram/Web напрямую."
        ),
        input_model=ArtifactSetDeliveryInput,
        progress_key="artifact_set_delivery",
        mutation=True,
    ),
)


class ArtifactDeliveryToolController:
    def __init__(self, service: ArtifactDeliveryService) -> None:
        self.service = service

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ManagerToolContext,
    ) -> ArtifactToolOutcome:
        if tool_name not in ARTIFACT_DELIVERY_TOOL_NAMES:
            return ArtifactToolOutcome(
                payload={
                    "type": "artifact_validation_error",
                    "code": "unknown_artifact_delivery_tool",
                    "message": "Unknown artifact delivery manager tool.",
                    "retryable": False,
                },
                event_type="artifact_validation_failed",
                severity="error",
            )
        try:
            parsed = ArtifactSetDeliveryInput.model_validate(arguments)
            client_type = getattr(context.client_type, "value", context.client_type)
            if not isinstance(client_type, str) or not client_type.strip():
                return ArtifactToolOutcome(
                    payload={
                        "type": "artifact_delivery_context_error",
                        "message": "Current client type is unavailable for delivery.",
                        "retryable": False,
                    },
                    event_type="artifact_validation_failed",
                    severity="error",
                )
            access = ArtifactAccessContext(
                session_id=context.session_id,
                cycle_id=context.cycle_id,
                allowed_artifact_ids=context.active_cycle.artifact_refs,
            )
            if parsed.selected:
                delivery = await self.service.select(
                    artifact_id=parsed.artifact_id,
                    access=access,
                    client_type=client_type,
                )
                return ArtifactToolOutcome(
                    payload={
                        "type": "artifact_delivery_selected",
                        "delivery": delivery.model_dump(mode="json"),
                    },
                    event_type="artifact_delivery_selected",
                    severity="success",
                    visibility="user",
                )

            records = await self.service.store.list_cycle(
                session_id=context.session_id,
                cycle_id=context.cycle_id,
            )
            matches = [
                item
                for item in records
                if item.artifact_id == parsed.artifact_id
                and item.client_type == client_type
                and item.state != ArtifactDeliveryState.CANCELLED
            ]
            if not matches:
                raise ArtifactDeliveryNotFoundError(
                    "No delivery selection exists for this artifact"
                )
            latest = matches[-1]
            cancelled = await self.service.cancel(latest.delivery_id)
            return ArtifactToolOutcome(
                payload={
                    "type": "artifact_delivery_cancelled",
                    "delivery": cancelled.model_dump(mode="json"),
                },
                event_type="artifact_delivery_cancelled",
                severity="success",
                visibility="user",
            )
        except ValidationError as error:
            return ArtifactToolOutcome(
                payload={
                    "type": "artifact_validation_error",
                    "code": "invalid_tool_arguments",
                    "message": "Artifact delivery arguments do not match the schema.",
                    "retryable": True,
                    "details": {"issue_count": error.error_count()},
                },
                event_type="artifact_validation_failed",
                severity="warning",
            )
        except (ArtifactNotFoundError, ArtifactDeliveryNotFoundError):
            return ArtifactToolOutcome(
                payload={
                    "type": "artifact_delivery_not_found",
                    "message": "Artifact delivery target was not found.",
                    "retryable": False,
                },
                event_type="artifact_validation_failed",
                severity="warning",
            )
        except ArtifactAccessError:
            return ArtifactToolOutcome(
                payload={
                    "type": "artifact_access_error",
                    "message": "Artifact is outside the current runtime authority.",
                    "retryable": False,
                },
                event_type="artifact_validation_failed",
                severity="error",
            )
        except ArtifactDeliveryError as error:
            return ArtifactToolOutcome(
                payload={
                    "type": "artifact_delivery_error",
                    "message": str(error),
                    "retryable": False,
                },
                event_type="artifact_validation_failed",
                severity="warning",
            )
        except (ArtifactStorageError, ArtifactIntegrityError):
            raise
