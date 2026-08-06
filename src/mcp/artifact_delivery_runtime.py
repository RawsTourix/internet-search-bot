"""Production composition of delivery, DAG planning, and finalization guards."""

from .artifact_access_scope import ArtifactAccessScopeMixin
from .artifact_composite_budget import ArtifactCompositeBudgetMixin
from .artifact_composite_compaction import ArtifactCompositeCompactionMixin
from .artifact_composite_preview import ArtifactCompositePreviewMixin
from .artifact_composite_recovery import ArtifactCompositeRecoveryMixin
from .artifact_delivery_client import ArtifactDeliveryMixin
from .artifact_delivery_progress import ArtifactDeliveryProgressMixin
from .artifact_trace_runtime import ArtifactLifecycleTraceMixin
from .input_runtime_checkpoints import InputRuntimeCheckpointMixin
from .llm_response_recovery import LLMResponseRecoveryMixin
from .planning_runtime import FinalizingPlanningMCPClient
from .waiting_user_batch_continuation import WaitingUserBatchContinuationMixin


class FinalizingArtifactDeliveryPlanningMCPClient(
    InputRuntimeCheckpointMixin,
    WaitingUserBatchContinuationMixin,
    LLMResponseRecoveryMixin,
    ArtifactCompositeBudgetMixin,
    ArtifactCompositeRecoveryMixin,
    ArtifactCompositePreviewMixin,
    ArtifactCompositeCompactionMixin,
    ArtifactDeliveryProgressMixin,
    ArtifactLifecycleTraceMixin,
    ArtifactDeliveryMixin,
    ArtifactAccessScopeMixin,
    FinalizingPlanningMCPClient,
):
    """Production agent with durable FIFO checkpoint application."""
