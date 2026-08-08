"""Process-local composition bridge for IR-7 final output coordination.

The durable authority remains in input-runtime repositories.  This module only
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
    if _final_output_assembler is not None and _final_output_assembler is not assembler:
        raise RuntimeError("final output assembler is already bound")
    _final_output_assembler = assembler


def get_final_output_assembler() -> Any | None:
    return _final_output_assembler


def bind_output_eligibility(checker: OutputEligibility) -> None:
    global _output_eligibility
    if _output_eligibility is not None and _output_eligibility != checker:
        raise RuntimeError("output eligibility checker is already bound")
    _output_eligibility = checker


async def output_delivery_allowed(batch: Any) -> bool:
    checker = _output_eligibility
    if checker is None:
        # Compatibility mode outside the input runtime keeps the pre-IR-7
        # delivery contract.  Production input-runtime composition binds the
        # durable finalization checker during startup.
        return True
    return bool(await checker(batch))


def clear_finalization_bridge_for_tests() -> None:
    global _final_output_assembler, _output_eligibility
    _final_output_assembler = None
    _output_eligibility = None
