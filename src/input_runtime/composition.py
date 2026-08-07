"""Process-local application binding assembled by the API composition root."""

from __future__ import annotations

from dataclasses import dataclass

from .admission import CommittedInputBatchReader
from .checkpoints import InputRuntimeCheckpointService
from .config import InputRuntimeConfigType
from .emissions import AgentEmissionService
from .factory import InputRuntimeRepositories
from .ir6_outbox import AgentEmissionOutboxService


@dataclass(frozen=True, slots=True)
class InputRuntimeApplicationBinding:
    config: InputRuntimeConfigType
    repositories: InputRuntimeRepositories
    committed_batches: CommittedInputBatchReader
    checkpoint_service: InputRuntimeCheckpointService
    emission_service: AgentEmissionService
    emission_outbox_service: AgentEmissionOutboxService


_binding: InputRuntimeApplicationBinding | None = None


def register_input_runtime_binding(
    *,
    config: InputRuntimeConfigType,
    repositories: InputRuntimeRepositories,
    committed_batches: CommittedInputBatchReader,
    checkpoint_service: InputRuntimeCheckpointService,
    emission_service: AgentEmissionService,
    emission_outbox_service: AgentEmissionOutboxService,
) -> InputRuntimeApplicationBinding:
    global _binding
    candidate = InputRuntimeApplicationBinding(
        config=config,
        repositories=repositories,
        committed_batches=committed_batches,
        checkpoint_service=checkpoint_service,
        emission_service=emission_service,
        emission_outbox_service=emission_outbox_service,
    )
    _binding = candidate
    return candidate


def get_input_runtime_binding() -> InputRuntimeApplicationBinding | None:
    return _binding


def clear_input_runtime_binding_for_tests() -> None:
    global _binding
    _binding = None
