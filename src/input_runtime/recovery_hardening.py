"""IR-8 conservative classification for legacy control-resume invocations."""

from __future__ import annotations

from .models import ControlCommandType, ControlState
from .recovery import (
    InputRuntimeRecoveryCoordinator as _BaseRecoveryCoordinator,
    InputRuntimeRecoveryPlan,
    RecoveryDisposition,
    RecoverySessionPlan,
)


class InputRuntimeRecoveryCoordinator(_BaseRecoveryCoordinator):
    """Do not infer pre-handoff safety for a dead `/continue` invocation.

    IR-5 predates per-control RuntimeHandoff records.  An applied/acknowledged
    CONTINUE followed by a durable RUNNING snapshot proves that continuation was
    authorized, but cannot prove whether its fresh process invocation crossed an
    external side-effect boundary.  Until stronger durable evidence exists, IR-8
    preserves that uncertainty and requires reset rather than blind replay.
    """

    async def recover(self) -> InputRuntimeRecoveryPlan:
        plan = await super().recover()
        hardened: list[RecoverySessionPlan] = []
        changed = 0
        for item in plan.sessions:
            if item.disposition != RecoveryDisposition.AUTO_RESUME_SAFE:
                hardened.append(item)
                continue
            controls = await self.repositories.controls.list_for_session(  # type: ignore[attr-defined]
                item.session_id
            )
            unsafe_continue = any(
                row.command == ControlCommandType.CONTINUE
                and row.target_cycle_id == item.cycle_id
                and row.generation == item.generation
                and row.state in {
                    ControlState.ACKNOWLEDGED,
                    ControlState.APPLIED,
                }
                for row in controls
            )
            if not unsafe_continue:
                hardened.append(item)
                continue
            changed += 1
            hardened.append(
                RecoverySessionPlan(
                    session_id=item.session_id,
                    cycle_id=item.cycle_id,
                    generation=item.generation,
                    disposition=RecoveryDisposition.AMBIGUOUS,
                    snapshot=item.snapshot,
                    reason_code="control_resume_side_effect_boundary_unknown",
                )
            )
        if changed:
            plan.report.resumable_cycles = max(
                0,
                plan.report.resumable_cycles - changed,
            )
            plan.report.handoffs_ambiguous += changed
        return InputRuntimeRecoveryPlan(
            sessions=tuple(hardened),
            report=plan.report,
        )
