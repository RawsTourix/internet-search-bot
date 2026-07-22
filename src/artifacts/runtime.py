"""Bounded runtime projection for artifact-aware agent iterations."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..mcp.manager_context import ManagerToolContext
from .delivery import ArtifactDeliveryService
from .models import (
    ArtifactAccessContext,
    ArtifactDeliveryRef,
    ArtifactVersionRef,
)
from .service import ArtifactService


class ArtifactRuntimeState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = "artifact_state"
    count: int = Field(ge=0)
    items: list[ArtifactVersionRef] = Field(default_factory=list)
    truncated: bool = False
    deliveries: list[ArtifactDeliveryRef] = Field(default_factory=list)


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
        all_items = await self.service.list_artifacts(
            access=access,
            limit=self.service.config.max_artifacts_per_cycle,
        )
        maximum = self.service.config.max_runtime_artifact_summaries
        deliveries: list[ArtifactDeliveryRef] = []
        if self.delivery_service is not None:
            deliveries = await self.delivery_service.list_cycle_refs(
                session_id=context.session_id,
                cycle_id=context.cycle_id,
            )
        state = ArtifactRuntimeState(
            count=len(all_items),
            items=all_items[:maximum],
            truncated=len(all_items) > maximum,
            deliveries=deliveries[:maximum],
        )
        context.active_cycle.artifact_state = state
        return state
