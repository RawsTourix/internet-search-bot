"""Public facade for v0.4 input-runtime contracts."""

from .admission import (
    AdmissionWakeCoordinator,
    CommittedInputBatchReader,
    InputAdmissionAction,
    InputAdmissionOutcome,
)
from .ir4_persistence_windows import DurableClaimCycleInputApplier as CycleInputApplier
from .ir4_checkpoint_contracts import EntryWatermarkCheckpointService as InputRuntimeCheckpointService
from .composition import (
    InputRuntimeApplicationBinding,
    clear_input_runtime_binding_for_tests,
    get_input_runtime_binding,
    register_input_runtime_binding,
)
from .config import (
    InputRuntimeConfigType,
    load_input_runtime_config,
    parse_input_runtime_config,
    safe_input_runtime_config_summary,
)
from .coordination import SessionLockRegistry
from .errors import (
    InputRuntimeConfigValidationError,
    InputRuntimeConflictError,
    InputRuntimeError,
    InputRuntimeNotFoundError,
)
from .factory import (
    InputRuntimeContracts,
    InputRuntimeRepositories,
    InputRuntimeRepositoryBundle,
    create_filesystem_input_runtime_repositories,
    create_input_runtime_contracts,
)
from .handoff import RuntimeHandoffRecord, RuntimeHandoffState
from .interfaces import RuntimeHandoffRepository
from .models import (
    ActiveCycleSnapshot, AdmissionKind, AdmissionState, AgentEmission,
    CheckpointAction, CheckpointName, CheckpointOutcome, ClaimedInboxRange,
    ControlCommandType, ControlOutcome, ControlState, CycleContextRevision,
    CycleFinalizationRecord, CycleInboxItem, CycleStatus, EmissionState,
    FinalizationState, InboxState, InputAdmissionRecord,
    SessionControlCommand, SessionInputRuntimeState, new_admission_id,
    new_context_revision_id, new_control_id, new_emission_id,
    new_finalization_id, new_inbox_item_id,
)
from .ir4_admission import InputAdmissionService
from .projection import (
    build_input_batch_update,
    build_input_batch_update_message,
    project_committed_batch,
)

__all__ = [name for name in globals() if not name.startswith("_")]
