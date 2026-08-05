"""Composition data for input-runtime repository implementations."""

from __future__ import annotations

from dataclasses import dataclass

from src.storage import StorageConfigType

from .config import InputRuntimeConfigType
from .interfaces import (
    ActiveCycleSnapshotRepository,
    AgentEmissionRepository,
    ContextRevisionRepository,
    CycleInboxRepository,
    FinalizationRepository,
    InputAdmissionRepository,
    SessionControlRepository,
    SessionInputRuntimeRepository,
)


@dataclass(frozen=True, slots=True)
class InputRuntimeRepositories:
    sessions: SessionInputRuntimeRepository
    admissions: InputAdmissionRepository
    inbox: CycleInboxRepository
    controls: SessionControlRepository
    snapshots: ActiveCycleSnapshotRepository
    context_revisions: ContextRevisionRepository
    emissions: AgentEmissionRepository
    finalizations: FinalizationRepository


@dataclass(frozen=True, slots=True)
class InputRuntimeContracts:
    config: InputRuntimeConfigType
    repositories: InputRuntimeRepositories


def create_input_runtime_contracts(
    *,
    config: InputRuntimeConfigType,
    repositories: InputRuntimeRepositories,
) -> InputRuntimeContracts:
    """Build the infrastructure-neutral contract bundle used by later stages."""
    return InputRuntimeContracts(config=config, repositories=repositories)


def create_filesystem_input_runtime_repositories(
    *,
    storage_config: StorageConfigType,
) -> InputRuntimeRepositories:
    """Build durable filesystem adapters without connecting them to the API."""
    from .filesystem import FileSystemInputRuntimeRepositories

    adapters = FileSystemInputRuntimeRepositories(storage_config)
    return InputRuntimeRepositories(
        sessions=adapters.sessions,
        admissions=adapters.admissions,
        inbox=adapters.inbox,
        controls=adapters.controls,
        snapshots=adapters.snapshots,
        context_revisions=adapters.context_revisions,
        emissions=adapters.emissions,
        finalizations=adapters.finalizations,
    )
