"""Process-local application binding assembled by the API composition root."""

from __future__ import annotations

from dataclasses import dataclass

from .admission import CommittedInputBatchReader
from .checkpoints import InputRuntimeCheckpointService
from .config import InputRuntimeConfigType
from .factory import InputRuntimeRepositories


@dataclass(frozen=True, slots=True)
class InputRuntimeApplicationBinding:
    config: InputRuntimeConfigType
    repositories: InputRuntimeRepositories
    committed_batches: CommittedInputBatchReader
    checkpoint_service: InputRuntimeCheckpointService


_binding: InputRuntimeApplicationBinding | None = None


def register_input_runtime_binding(
    *,
    config: InputRuntimeConfigType,
    repositories: InputRuntimeRepositories,
    committed_batches: CommittedInputBatchReader,
    checkpoint_service: InputRuntimeCheckpointService,
) -> InputRuntimeApplicationBinding:
    global _binding
    candidate = InputRuntimeApplicationBinding(
        config=config,
        repositories=repositories,
        committed_batches=committed_batches,
        checkpoint_service=checkpoint_service,
    )
    _binding = candidate
    return candidate


def get_input_runtime_binding() -> InputRuntimeApplicationBinding | None:
    return _binding


def clear_input_runtime_binding_for_tests() -> None:
    global _binding
    _binding = None
