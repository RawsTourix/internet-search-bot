"""Bounded runtime projection for artifact-aware agent iterations."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..mcp.manager_context import ManagerToolContext
from .delivery import ArtifactDeliveryService
from .models import (
    ArtifactAccessContext,
    ArtifactCatalogItem,
    ArtifactDeliveryRef,
)
from .service import ArtifactService
from ..interaction.parts import (
    ArtifactDeliverableProjection,
    ArtifactInputManifest,
    ArtifactManifestItem,
)


class ArtifactRuntimeState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = "artifact_state"
    available_count: int = Field(ge=0)
    lineage_count: int = Field(ge=0)
    items: list[ArtifactCatalogItem] = Field(default_factory=list)
    items_truncated: bool = False
    deliveries: list[ArtifactDeliveryRef] = Field(default_factory=list)
    input_manifest: ArtifactInputManifest = Field(
        default_factory=lambda: ArtifactInputManifest(available_count=0)
    )
    deliverable_projection: ArtifactDeliverableProjection = Field(
        default_factory=ArtifactDeliverableProjection
    )

    @property
    def count(self) -> int:
        """Compatibility accessor for internal pre-catalog callers."""

        return self.available_count

    @property
    def truncated(self) -> bool:
        return self.items_truncated


class ArtifactRuntimeCoordinator:
    """Rebuild compact state from exact runtime-owned artifact references."""

    def __init__(
        self,
        service: ArtifactService,
        delivery_service: ArtifactDeliveryService | None = None,
    ) -> None:
        self.service = service
        self.delivery_service = delivery_service

    async def refresh(
        self,
        context: ManagerToolContext,
    ) -> ArtifactRuntimeState:
        access = ArtifactAccessContext(
            session_id=context.session_id,
            cycle_id=context.cycle_id,
            allowed_artifact_ids=context.active_cycle.artifact_refs,
        )
        deliveries: list[ArtifactDeliveryRef] = []
        if self.delivery_service is not None:
            deliveries = await self.delivery_service.list_cycle_refs(
                session_id=context.session_id,
                cycle_id=context.cycle_id,
            )
        read_ids = list(context.active_cycle.read_artifact_refs)
        for event in context.active_cycle.cycle_trace:
            if event.get("type") != "artifact_read_completed":
                continue
            for artifact_id in event.get("artifact_ids") or []:
                if artifact_id not in read_ids:
                    read_ids.append(artifact_id)
        context.active_cycle.read_artifact_refs = read_ids
        catalog = await self.service.catalog_artifacts(
            access=access,
            limit=self.service.config.max_artifacts_per_cycle,
            read_artifact_ids=read_ids,
            deliveries=deliveries,
        )
        activation_by_id = self._activation_by_id(context)
        maximum = self.service.config.max_runtime_artifact_summaries
        manifest_items = tuple(
            ArtifactManifestItem(
                artifact_id=item.artifact_id,
                artifact_lineage_id=item.artifact_lineage_id,
                version=item.version,
                filename=item.filename,
                format_id=item.format_id,
                mime_type=item.mime_type,
                size_bytes=item.size_bytes,
                purpose=item.purpose.value,
                capabilities=tuple(
                    key
                    for key, enabled in item.capabilities.model_dump().items()
                    if enabled
                ),
                activation_reason=self._activation_value(
                    activation_by_id.get(item.artifact_id),
                    "reason",
                    fallback=(
                        "created_in_cycle"
                        if item.created_in_current_cycle
                        else "current_input_batch"
                    ),
                ),
                activation_scope=self._activation_value(
                    activation_by_id.get(item.artifact_id),
                    "scope",
                    fallback="current",
                ),
                activation_source_operation_id=self._activation_value(
                    activation_by_id.get(item.artifact_id),
                    "source_operation_id",
                ),
            )
            for item in catalog.items[:maximum]
        )
        selected_ids = tuple(item.artifact_id for item in deliveries)
        deliverable_items = tuple(
            item
            for item in manifest_items
            if item.purpose == "deliverable"
        )
        state = ArtifactRuntimeState(
            available_count=catalog.available_count,
            lineage_count=catalog.lineage_count,
            items=catalog.items[:maximum],
            items_truncated=(
                catalog.items_truncated
                or catalog.available_count > maximum
            ),
            deliveries=deliveries[:maximum],
            input_manifest=ArtifactInputManifest(
                items=manifest_items,
                available_count=catalog.available_count,
                truncated=(
                    catalog.items_truncated
                    or catalog.available_count > maximum
                ),
            ),
            deliverable_projection=ArtifactDeliverableProjection(
                created_deliverables=deliverable_items,
                selected_artifact_ids=selected_ids,
                unselected_artifact_ids=tuple(
                    item.artifact_id
                    for item in deliverable_items
                    if item.artifact_id not in selected_ids
                ),
                delivery_states={
                    item.artifact_id: item.state.value
                    for item in deliveries
                },
            ),
        )
        context.active_cycle.artifact_state = state
        return state

    @staticmethod
    def _activation_by_id(context: ManagerToolContext) -> dict[str, dict]:
        result: dict[str, dict] = {}
        for item in getattr(context.active_cycle, "artifact_activations", []) or []:
            if not isinstance(item, dict):
                continue
            artifact_id = str(item.get("artifact_id") or "")
            if artifact_id and artifact_id not in result:
                result[artifact_id] = item
        return result

    @staticmethod
    def _activation_value(
        record: dict | None,
        key: str,
        *,
        fallback: str | None = None,
    ) -> str | None:
        if record is None:
            return fallback
        value = record.get(key)
        if hasattr(value, "value"):
            value = value.value
        normalized = str(value).strip() if value is not None else ""
        return normalized or fallback
