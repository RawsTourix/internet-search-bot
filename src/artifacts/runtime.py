"""Bounded runtime projection for artifact-aware agent iterations."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..mcp.manager_context import ManagerToolContext
from .models import ArtifactAccessContext, ArtifactVersionRef
from .service import ArtifactService


class ArtifactRuntimeState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = "artifact_state"
    count: int = Field(ge=0)
    items: list[ArtifactVersionRef] = Field(default_factory=list)
    truncated: bool = False


class ArtifactRuntimeCoordinator:
    """Rebuild compact state from exact runtime-owned artifact references."""

    def __init__(self, service: ArtifactService) -> None:
        self.service = service

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
        state = ArtifactRuntimeState(
            count=len(all_items),
            items=all_items[:maximum],
            truncated=len(all_items) > maximum,
        )
        context.active_cycle.artifact_state = state
        return state
