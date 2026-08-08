"""IR-8 terminal-authority validation layered on conservative recovery."""

from __future__ import annotations

from .handoff import RuntimeHandoffState
from .models import CycleStatus, FinalizationState
from .recovery import InputRuntimeRecoveryPlan
from .recovery_hardening import (
    InputRuntimeRecoveryCoordinator as _HardenedRecoveryCoordinator,
)


class InputRuntimeRecoveryCoordinator(_HardenedRecoveryCoordinator):
    """Reject terminal projections that are not backed by terminal authority."""

    async def _recover(self, report) -> InputRuntimeRecoveryPlan:
        plan = await super()._recover(report)
        records = await self.repositories.finalizations.list_for_recovery()  # type: ignore[attr-defined]
        terminal = [
            item
            for item in records
            if item.state == FinalizationState.TERMINAL_COMMITTED
        ]

        by_cycle: dict[tuple[str, str, int], list[object]] = {}
        for record in terminal:
            key = (record.session_id, record.cycle_id, record.generation)
            by_cycle.setdefault(key, []).append(record)
            await self._validate_output_ready_evidence(record)
            marker = await self._handoff_for_finalization(record)
            if marker is None or marker.state != RuntimeHandoffState.COMPLETED:
                raise self._fatal("terminal_handoff_not_completed")
            snapshot = await self.repositories.snapshots.get(record.cycle_id)
            if (
                snapshot is None
                or snapshot.session_id != record.session_id
                or snapshot.generation != record.generation
                or snapshot.status != CycleStatus.DONE
            ):
                raise self._fatal("terminal_snapshot_projection_mismatch")

        for state in await self.repositories.sessions.list_states():
            if state.cycle_status != CycleStatus.DONE:
                continue
            if state.active_cycle_id is None:
                raise self._fatal("terminal_session_cycle_missing")
            matches = by_cycle.get(
                (state.session_id, state.active_cycle_id, state.generation),
                [],
            )
            if len(matches) != 1:
                raise self._fatal("terminal_session_without_authoritative_marker")
            if state.finalization_id != matches[0].finalization_id:
                raise self._fatal("terminal_session_finalization_mismatch")

        return plan
