"""Explicit production composition for IR-8 recovery types."""

from __future__ import annotations

from . import input_runtime_recovery as lifecycle
from .ir8_final_output_recovery import FinalOutputRecovery
from ..input_runtime.recovery_terminal import (
    InputRuntimeRecoveryCoordinator as ProductionInputRuntimeRecoveryCoordinator,
)


def install_production_recovery_types() -> None:
    """Bind the complete IR-8 recovery chain used by production Api startup."""

    lifecycle.InputRuntimeRecoveryCoordinator = (
        ProductionInputRuntimeRecoveryCoordinator
    )
    lifecycle._FinalOutputRecovery = FinalOutputRecovery
