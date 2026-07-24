"""Production composition of delivery, DAG planning, and finalization guards."""

from .artifact_composite_budget import ArtifactCompositeBudgetMixin
from .artifact_composite_compaction import ArtifactCompositeCompactionMixin
from .artifact_composite_preview import ArtifactCompositePreviewMixin
from .artifact_delivery_client import ArtifactDeliveryMixin
from .planning_runtime import FinalizingPlanningMCPClient


class FinalizingArtifactDeliveryPlanningMCPClient(
    ArtifactCompositeBudgetMixin,
    ArtifactCompositePreviewMixin,
    ArtifactCompositeCompactionMixin,
    ArtifactDeliveryMixin,
    FinalizingPlanningMCPClient,
):
    """Production agent client with planning and durable artifact delivery."""
