"""Composition data for input-runtime repository implementations."""

from __future__ import annotations

from dataclasses import dataclass

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
