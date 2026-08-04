"""Manager tool for explicit artifact delivery selection."""

from __future__ import annotations

from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from ..mcp.artifact_request_context import get_artifact_request_client_type
from ..mcp.manager_context import ManagerToolContext
from .delivery import ArtifactDeliveryService
from .errors import (
    ArtifactAccessError,
    ArtifactDeliveryError,
    ArtifactDeliveryNotFoundError,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactStorageError,
    ArtifactValidationError,
)
from .models import (
    ArtifactAccessContext,
    ArtifactDeliveryBatchItem,
    ArtifactDeliveryBatchResult,
    is_artifact_id,
)
from .tools import (
    ArtifactResultPolicy,
    ArtifactToolDefinition,
    ArtifactToolOutcome,
    ToolExecutionDisposition,
)


ARTIFACT_DELIVERY_TOOL_NAMES = frozenset({"artifact_set_delivery"})


class ArtifactSetDeliveryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_ids: list[str] = Field(min_length=1)
    selected: bool = True
    redeliver_input: bool = False

    @field_validator("artifact_ids")
    @classmethod
    def normalize_artifact_ids(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            normalized = value.strip()
            if not normalized:
                raise ValueError("artifact_ids must not contain empty strings")
            result.append(normalized)
        return result


ARTIFACT_DELIVERY_TOOL_DEFINITIONS = (
    ArtifactToolDefinition(
        name="artifact_set_delivery",
        description=(
            "Атомарно выбрать список exact immutable artifact_ids для "
            "доставки текущему клиенту либо отменить весь список. Один файл "
            "тоже передаётся списком. selected означает durable selection, "
            "а не подтверждённую transport delivery. Входной user_upload "
            "запрещён по умолчанию: redeliver_input=true допустим только при "
            "явной просьбе пользователя вернуть входной файл."
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
                disposition=ToolExecutionDisposition.REJECTED,
                result_policy=ArtifactResultPolicy.INLINE_RECEIPT,
            )
        try:
            parsed = ArtifactSetDeliveryInput.model_validate(arguments)
            if (
                len(parsed.artifact_ids)
                > self.service.config.max_artifacts_per_cycle
            ):
                return self._rejected(
                    parsed.artifact_ids,
                    code="artifact_limit_error",
                    message=(
                        "Artifact delivery batch exceeds "
                        "max_artifacts_per_cycle."
                    ),
                    retryable=True,
                )
            if any(
                not is_artifact_id(artifact_id)
                for artifact_id in parsed.artifact_ids
            ):
                return self._rejected(
                    parsed.artifact_ids,
                    code="invalid_artifact_id",
                    message=(
                        "One or more artifact IDs are invalid. Call "
                        "artifact_list and retry with exact artifact_ids."
                    ),
                    retryable=True,
                )
            if (
                parsed.selected
                and not parsed.redeliver_input
                and all(
                    artifact_id in context.active_cycle.artifact_refs
                    for artifact_id in parsed.artifact_ids
                )
                and await self._contains_input_artifact(parsed.artifact_ids)
            ):
                return self._rejected(
                    parsed.artifact_ids,
                    code="input_redelivery_requires_explicit_request",
                    message=(
                        "Input artifacts are not delivery outputs by default. "
                        "Set redeliver_input=true only when the user's current "
                        "request explicitly asks to send those input files back."
                    ),
                    retryable=False,
                )
            raw_client_type = (
                context.client_type
                if context.client_type is not None
                else get_artifact_request_client_type()
            )
            client_type = getattr(raw_client_type, "value", raw_client_type)
            if not isinstance(client_type, str) or not client_type.strip():
                return self._rejected(
                    parsed.artifact_ids,
                    code="artifact_delivery_context_error",
                    message=(
                        "Current client type is unavailable for delivery."
                    ),
                    retryable=False,
                )
            access = ArtifactAccessContext(
                session_id=context.session_id,
                cycle_id=context.cycle_id,
                allowed_artifact_ids=context.active_cycle.artifact_refs,
            )
            if parsed.selected:
                deliveries = await self.service.select_many(
                    artifact_ids=parsed.artifact_ids,
                    access=access,
                    client_type=client_type,
                )
                result = ArtifactDeliveryBatchResult(
                    type="artifact_delivery_batch_selected",
                    status="selected",
                    requested_count=len(parsed.artifact_ids),
                    selected_count=len(parsed.artifact_ids),
                    cancelled_count=0,
                    items=[
                        ArtifactDeliveryBatchItem(
                            request_index=index,
                            requested_artifact_id=artifact_id,
                            status="selected",
                            artifact_id=delivery.artifact_id,
                            filename=delivery.filename,
                            delivery_id=delivery.delivery_id,
                            state=delivery.state,
                        )
                        for index, (artifact_id, delivery) in enumerate(
                            zip(
                                parsed.artifact_ids,
                                deliveries,
                                strict=True,
                            )
                        )
                    ],
                )
                return ArtifactToolOutcome(
                    payload=result.model_dump(mode="json"),
                    event_type="artifact_delivery_selected",
                    severity="success",
                    visibility="user",
                    result_policy=ArtifactResultPolicy.INLINE_RECEIPT,
                )

            deliveries = await self.service.cancel_many_by_artifact_ids(
                artifact_ids=parsed.artifact_ids,
                access=access,
                client_type=client_type,
            )
            result = ArtifactDeliveryBatchResult(
                type="artifact_delivery_batch_cancelled",
                status="cancelled",
                requested_count=len(parsed.artifact_ids),
                selected_count=0,
                cancelled_count=len(parsed.artifact_ids),
                items=[
                    ArtifactDeliveryBatchItem(
                        request_index=index,
                        requested_artifact_id=artifact_id,
                        status="cancelled",
                        artifact_id=delivery.artifact_id,
                        filename=delivery.filename,
                        delivery_id=delivery.delivery_id,
                        state=delivery.state,
                    )
                    for index, (artifact_id, delivery) in enumerate(
                        zip(
                            parsed.artifact_ids,
                            deliveries,
                            strict=True,
                        )
                    )
                ],
            )
            return ArtifactToolOutcome(
                payload=result.model_dump(mode="json"),
                event_type="artifact_delivery_cancelled",
                severity="success",
                visibility="user",
                result_policy=ArtifactResultPolicy.INLINE_RECEIPT,
            )
        except ValidationError as error:
            return self._rejected(
                [],
                code="invalid_tool_arguments",
                message=(
                    "Artifact delivery arguments do not match the schema "
                    f"({error.error_count()} issue(s))."
                ),
                retryable=True,
            )
        except (
            ArtifactNotFoundError,
            ArtifactDeliveryNotFoundError,
            ArtifactAccessError,
        ):
            return self._rejected(
                parsed.artifact_ids,
                code="artifact_access_error",
                message=(
                    "One or more delivery targets are not accessible from "
                    "the current runtime authority."
                ),
                retryable=True,
            )
        except ArtifactValidationError as error:
            return self._rejected(
                parsed.artifact_ids,
                code=error.code,
                message=error.safe_message,
                retryable=error.retryable,
            )
        except ArtifactDeliveryError as error:
            return self._rejected(
                parsed.artifact_ids,
                code="artifact_delivery_error",
                message=str(error),
                retryable=False,
            )
        except (ArtifactStorageError, ArtifactIntegrityError):
            raise

    async def _contains_input_artifact(self, artifact_ids: list[str]) -> bool:
        store = self.service.artifact_service.artifact_store
        for artifact_id in dict.fromkeys(artifact_ids):
            version = await store.get_version(artifact_id)
            if getattr(version.provenance, "origin", None) == "user_upload":
                return True
        return False

    @staticmethod
    def _rejected(
        artifact_ids: list[str],
        *,
        code: str,
        message: str,
        retryable: bool,
    ) -> ArtifactToolOutcome:
        result = ArtifactDeliveryBatchResult(
            type="artifact_delivery_batch_rejected",
            status="rejected",
            requested_count=len(artifact_ids),
            selected_count=0,
            cancelled_count=0,
            items=[
                ArtifactDeliveryBatchItem(
                    request_index=index,
                    requested_artifact_id=artifact_id,
                    status="rejected",
                    code=(
                        code
                        if code == "invalid_artifact_id"
                        and not is_artifact_id(artifact_id)
                        else "atomic_batch_rejected"
                    ),
                    message=message,
                    retryable=retryable,
                    suggested_action=(
                        "Call artifact_list and retry with exact artifact_ids."
                    ),
                )
                for index, artifact_id in enumerate(artifact_ids)
            ],
        )
        return ArtifactToolOutcome(
            payload=result.model_dump(mode="json"),
            event_type="artifact_validation_failed",
            severity="warning",
            disposition=ToolExecutionDisposition.REJECTED,
            result_policy=ArtifactResultPolicy.INLINE_RECEIPT,
        )
