"""Production composition of delivery, DAG planning, and finalization guards."""

from .artifact_composite_compaction import ArtifactCompositeCompactionMixin
from .artifact_composite_preview import ArtifactCompositePreviewMixin
from .artifact_delivery_client import ArtifactDeliveryMixin
from .planning_runtime import FinalizingPlanningMCPClient


class FinalizingArtifactDeliveryPlanningMCPClient(
    ArtifactCompositePreviewMixin,
    ArtifactCompositeCompactionMixin,
    ArtifactDeliveryMixin,
    FinalizingPlanningMCPClient,
):
    """Production agent client with planning and durable artifact delivery."""
