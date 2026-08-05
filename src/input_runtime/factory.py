"""Composition contracts for future input-runtime implementations."""
from dataclasses import dataclass
from .config import InputRuntimeConfigType
from .interfaces import (
    ActiveCycleSnapshotRepository, AgentEmissionRepository,
    ContextRevisionRepository, CycleInboxRepository, FinalizationRepository,
    InputAdmissionRepository, SessionControlRepository,
    SessionInputRuntimeRepository,
)

@dataclass(frozen=True, slots=True)
class InputRuntimeRepositoryBundle:
    sessions: SessionInputRuntimeRepository
    admissions: InputAdmissionRepository
    inbox: CycleInboxRepository
    controls: SessionControlRepository
    snapshots: ActiveCycleSnapshotRepository
    context_revisions: ContextRevisionRepository
    emissions: AgentEmissionRepository
    finalizations: FinalizationRepository

@dataclass(frozen=True, slots=True)
class InputRuntimeDependencies:
    config: InputRuntimeConfigType
    repositories: InputRuntimeRepositoryBundle
