"""Production composition of delivery, DAG planning, and finalization guards."""

from .artifact_delivery_client import ArtifactDeliveryMixin
from .planning_runtime import FinalizingPlanningMCPClient


class FinalizingArtifactDeliveryPlanningMCPClient(
    ArtifactDeliveryMixin,
    FinalizingPlanningMCPClient,
):
    """Production agent client with planning and durable artifact delivery."""
