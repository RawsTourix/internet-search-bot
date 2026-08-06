"""Process-local composition binding for IR-4 runtime services.

The binding contains application ports/configuration only.  It deliberately does
not expose filesystem adapters or transport state to the agent loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .admission import CommittedInputBatchReader
from .config import InputRuntimeConfigType
from .factory import InputRuntimeRepositories


@dataclass(frozen=True, slots=True)
class InputRuntimeApplicationBinding:
    config: InputRuntimeConfigType
    repositories: InputRuntimeRepositories
    committed_batches: CommittedInputBatchReader


_binding: InputRuntimeApplicationBinding | None = None


def register_input_runtime_binding(
    *,
    config: InputRuntimeConfigType,
    repositories: InputRuntimeRepositories,
    committed_batches: CommittedInputBatchReader,
) -> InputRuntimeApplicationBinding:
    global _binding
    candidate = InputRuntimeApplicationBinding(
        config=config,
        repositories=repositories,
        committed_batches=committed_batches,
    )
    _binding = candidate
    return candidate


def get_input_runtime_binding() -> InputRuntimeApplicationBinding | None:
    return _binding


def clear_input_runtime_binding_for_tests() -> None:
    global _binding
    _binding = None
