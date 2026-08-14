"""IR-8 terminal-authority validation layered on conservative recovery."""

from __future__ import annotations

from .handoff import RuntimeHandoffState
from .models import (
    ControlCommandType,
    ControlState,
    CycleStatus,
    FinalizationState,
)
from .recovery import InputRuntimeRecoveryPlan
from .recovery_hardening import (
    InputRuntimeRecoveryCoordinator as _HardenedRecoveryCoordinator,
)


_TERMINAL_CONTROL_STATES = {
    ControlState.APPLIED,
    ControlState.REJECTED,
    ControlState.CANCELLED,
}


class InputRuntimeRecoveryCoordinator(_HardenedRecoveryCoordinator):
    """Production IR-8 recovery with strict terminal and reset ordering."""

    @staticmethod
    def _safe_conflict_reason(error: Exception) -> str:
        text = str(error)
        canonical = {
            "duplicate authoritative session sequence": "duplicate_admission_sequence",
            "duplicate session admission sequence": "duplicate_admission_sequence",
            "duplicate authoritative active-cycle sequence": "duplicate_cycle_admission_sequence",
            "duplicate cycle admission sequence": "duplicate_cycle_admission_sequence",
            "gap in authoritative session sequence": "admission_sequence_gap",
            "gap in authoritative active-cycle sequence": "cycle_admission_sequence_gap",
        }
        for fragment, code in canonical.items():
            if fragment in text:
                return code
        return _HardenedRecoveryCoordinator._safe_conflict_reason(error)

    async def _preflight_admission_sequences(self) -> None:
        """Classify immutable sequence contradictions before adapter repair."""

        admissions = await self.repositories.admissions.list_all_for_recovery()  # type: ignore[attr-defined]
        by_session: dict[str, list[object]] = {}
        by_cycle: dict[tuple[str, int, str], list[object]] = {}
        for admission in admissions:
            by_session.setdefault(admission.session_id, []).append(admission)
            by_cycle.setdefault(
                (
                    admission.session_id,
                    admission.admitted_generation,
                    admission.target_cycle_id,
                ),
                [],
            ).append(admission)

        for rows in by_session.values():
            sequences = sorted(item.session_sequence for item in rows)
            if len(sequences) != len(set(sequences)):
                raise self._fatal("duplicate_admission_sequence")
            if sequences and sequences != list(range(1, sequences[-1] + 1)):
                raise self._fatal("admission_sequence_gap")

        for rows in by_cycle.values():
            sequences = sorted(item.cycle_sequence for item in rows)
            if len(sequences) != len(set(sequences)):
                raise self._fatal("duplicate_cycle_admission_sequence")
            if sequences and sequences != list(range(0, sequences[-1] + 1)):
                raise self._fatal("cycle_admission_sequence_gap")

    async def _repair_identity_and_frontiers(self, states) -> None:
        # Sequence contradictions are immutable-history corruption, not derived
        # index/watermark lag. Give them stable typed startup reasons before any
        # adapter repair is allowed to mutate derived state.
        await self._preflight_admission_sequences()
        await super()._repair_identity_and_frontiers(states)

    async def _terminal_records(self):
        records = await self.repositories.finalizations.list_for_recovery()  # type: ignore[attr-defined]
        return tuple(
            item
            for item in records
            if item.state == FinalizationState.TERMINAL_COMMITTED
        )

    async def _preflight_existing_terminal_authority(self) -> None:
        """Reject contradictory terminal authority before any recovery mutation."""

        for record in await self._terminal_records():
            marker = await self._handoff_for_finalization(record)
            if marker is None or marker.state != RuntimeHandoffState.COMPLETED:
                raise self._fatal("terminal_handoff_not_completed")
            await self._validate_output_ready_evidence(record)

    async def _recover_reset_transitions(self, report) -> None:
        """Finish already-durable RESET semantics before stale snapshot checks."""

        for state in await self.repositories.sessions.list_states():
            rows = await self.repositories.controls.list_for_session(  # type: ignore[attr-defined]
                state.session_id
            )
            for row in rows:
                if (
                    row.command == ControlCommandType.RESET
                    and row.state not in _TERMINAL_CONTROL_STATES
                ):
                    await self.admission_service.recover_reset_command(row)
                    report.controls_reconciled += 1

    async def _reconcile_snapshot_first_apply(self, snapshots, now, report) -> None:
        # A durable reset may already have advanced session.generation while its
        # old-generation cleanup was interrupted. Complete that exact existing
        # reset first; only then discover current active snapshots and apply the
        # ordinary strict snapshot validation/reconciliation rules.
        await self._recover_reset_transitions(report)
        current_snapshots = await self.repositories.snapshots.list_active()
        await super()._reconcile_snapshot_first_apply(
            current_snapshots,
            now,
            report,
        )

    async def _recover(self, report) -> InputRuntimeRecoveryPlan:
        # TERMINAL_COMMITTED is an irreversible authority marker. If its exact
        # handoff/output prerequisites contradict it, startup must fail before
        # base recovery gets an opportunity to repair projections or complete a
        # handoff retroactively.
        await self._preflight_existing_terminal_authority()

        plan = await super()._recover(report)
        terminal = await self._terminal_records()

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
