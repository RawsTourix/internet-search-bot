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


class ArtifactRuntimeState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = "artifact_state"
    available_count: int = Field(ge=0)
    lineage_count: int = Field(ge=0)
    items: list[ArtifactCatalogItem] = Field(default_factory=list)
    items_truncated: bool = False
    deliveries: list[ArtifactDeliveryRef] = Field(default_factory=list)

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
        maximum = self.service.config.max_runtime_artifact_summaries
        state = ArtifactRuntimeState(
            available_count=catalog.available_count,
            lineage_count=catalog.lineage_count,
            items=catalog.items[:maximum],
            items_truncated=(
                catalog.items_truncated
                or catalog.available_count > maximum
            ),
            deliveries=deliveries[:maximum],
        )
        context.active_cycle.artifact_state = state
        return state
