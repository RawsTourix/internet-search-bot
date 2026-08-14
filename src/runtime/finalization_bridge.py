"""Process-local composition bridge for IR-7 final output coordination.

The durable authority remains in input-runtime repositories. This module only
connects already-composed application services without introducing transport or
filesystem knowledge into the finalization service.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any


OutputEligibility = Callable[[Any], Awaitable[bool]]

_final_output_assembler: Any | None = None
_output_eligibility: OutputEligibility | None = None


def bind_final_output_assembler(assembler: Any) -> None:
    global _final_output_assembler
    # Api instances are process-local composition roots. Tests and embedded
    # runtimes may recreate one sequentially in the same interpreter.
    _final_output_assembler = assembler


def get_final_output_assembler() -> Any | None:
    return _final_output_assembler


def bind_output_eligibility(checker: OutputEligibility) -> None:
    global _final_output_assembler, _output_eligibility
    # InputAdmissionService is composed before OutputBatchAssembler. Clearing
    # the prior assembler here makes the later assembler bind the activation
    # edge for this exact composition root instead of inheriting stale process
    # state from an earlier Api/test runtime.
    _final_output_assembler = None
    _output_eligibility = checker


async def output_delivery_allowed(batch: Any) -> bool:
    checker = _output_eligibility
    if checker is None or _final_output_assembler is None:
        # Compatibility mode outside a complete IR-7 composition keeps the
        # pre-IR-7 delivery contract. Production Api composition always binds
        # admission/finalization authority first and the final assembler next.
        return True
    return bool(await checker(batch))


def clear_finalization_bridge_for_tests() -> None:
    global _final_output_assembler, _output_eligibility
    _final_output_assembler = None
    _output_eligibility = None
