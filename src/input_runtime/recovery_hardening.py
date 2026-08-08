"""IR-8 conservative recovery hardening over earlier runtime stages."""

from __future__ import annotations

from .models import ControlCommandType, ControlState, FinalizationState
from .recovery import (
    InputRuntimeRecoveryCoordinator as _BaseRecoveryCoordinator,
    InputRuntimeRecoveryPlan,
    RecoveryDisposition,
    RecoverySessionPlan,
)


class InputRuntimeRecoveryCoordinator(_BaseRecoveryCoordinator):
    """Preserve unknown side effects and terminal delivery fences on restart."""

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

        cancelled = 0
        finalizations = await self.repositories.finalizations.list_for_recovery()  # type: ignore[attr-defined]
        reconcile = getattr(
            self.repositories.emissions,
            "reconcile_terminal_ready_for_recovery",
            None,
        )
        if callable(reconcile):
            for record in finalizations:
                if record.state != FinalizationState.TERMINAL_COMMITTED:
                    continue
                rows = await reconcile(
                    session_id=record.session_id,
                    cycle_id=record.cycle_id,
                    generation=record.generation,
                )
                cancelled += len(rows)
        if cancelled:
            plan.report.emissions_cancelled += cancelled
            plan.report.emissions_retained = max(
                0,
                plan.report.emissions_retained - cancelled,
            )

        return InputRuntimeRecoveryPlan(
            sessions=tuple(hardened),
            report=plan.report,
        )
