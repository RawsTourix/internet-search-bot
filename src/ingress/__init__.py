"""Public API for transport-neutral file ingress."""

from .compat import legacy_message_to_input_envelope
from .config import (
    IngressConfigType,
    IngressConfigValidationError,
    load_ingress_config,
)
from .factory import IngressServices, create_ingress_services
from .models import (
    ClientAttachmentLocator,
    ClientConversationRef,
    ClientIngressEvent,
    ClientInputEnvelope,
    ClientReplyContext,
    ClientResponseRoute,
    ClientSenderRef,
    CommittedInputBatch,
    IngressAttachmentSlot,
    IngressTextPart,
    InputAdmissionMode,
    InputAttachmentPart,
    InputAttachmentState,
    InputBatchDraft,
    InputBatchDraftState,
    InputGroupingMode,
    InputSubmissionResult,
    is_ingress_event_id,
    is_input_batch_id,
    new_ingress_event_id,
    new_input_batch_id,
)
from .routing import (
    InputGroupingAmbiguityError,
    InputGroupingDecision,
    resolve_input_grouping,
)
from .service import ArtifactIngressService, IngressValidationError
from .store import (
    FileSystemIngressEventStore,
    FileSystemInputBatchStore,
    IngressConflictError,
    IngressNotFoundError,
)

__all__ = [
    "ArtifactIngressService",
    "ClientAttachmentLocator",
    "ClientConversationRef",
    "ClientIngressEvent",
    "ClientInputEnvelope",
    "ClientReplyContext",
    "ClientResponseRoute",
    "ClientSenderRef",
    "CommittedInputBatch",
    "FileSystemIngressEventStore",
    "FileSystemInputBatchStore",
    "IngressAttachmentSlot",
    "IngressConfigType",
    "IngressConfigValidationError",
    "IngressConflictError",
    "IngressNotFoundError",
    "IngressServices",
    "IngressTextPart",
    "IngressValidationError",
    "InputAdmissionMode",
    "InputAttachmentPart",
    "InputAttachmentState",
    "InputBatchDraft",
    "InputBatchDraftState",
    "InputGroupingAmbiguityError",
    "InputGroupingDecision",
    "InputGroupingMode",
    "InputSubmissionResult",
    "create_ingress_services",
    "is_ingress_event_id",
    "is_input_batch_id",
    "legacy_message_to_input_envelope",
    "load_ingress_config",
    "new_ingress_event_id",
    "new_input_batch_id",
    "resolve_input_grouping",
]
