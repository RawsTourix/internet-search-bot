"""IR-8 conservative recovery hardening over earlier runtime stages."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .handoff import RuntimeHandoffState
from .models import (
    AdmissionState,
    CheckpointName,
    ControlCommandType,
    ControlState,
    CycleStatus,
    FinalizationState,
    InboxState,
)
from .recovery import (
    InputRuntimeRecoveryCoordinator as _BaseRecoveryCoordinator,
    InputRuntimeRecoveryError,
    InputRuntimeRecoveryPlan,
    RecoveryDisposition,
    RecoverySessionPlan,
)


@dataclass(slots=True)
class _RecoveryControlCycle:
    session_id: str
    cycle_id: str
    input_runtime_generation: int


class InputRuntimeRecoveryCoordinator(_BaseRecoveryCoordinator):
    """Preserve unknown side effects and terminal delivery fences on restart.

    The production coordinator intentionally re-exposes the two base recovery
    steps that previously reached into IR-4/IR-5 private helpers.  Startup now
    crosses only repository ports and explicit public recovery commands on the
    application service, keeping the orchestration storage/transport neutral.
    """

    async def _validate_output_ready_evidence(self, record) -> None:
        validator = getattr(self.final_output_recovery, "validate_output_ready", None)
        if callable(validator):
            await validator(record)

    async def _preconverge_completed_terminal(self, finalizations, now, report) -> None:
        for record in finalizations:
            if record.state != FinalizationState.OUTPUT_READY:
                continue
            marker = await self._handoff_for_finalization(record)
            if marker is not None and marker.state == RuntimeHandoffState.COMPLETED:
                await self._validate_output_ready_evidence(record)
        await super()._preconverge_completed_terminal(finalizations, now, report)

    async def _reconcile_snapshot_first_apply(self, snapshots, now, report) -> None:
        for snapshot in snapshots:
            await self._validate_snapshot(snapshot, allow_session_lag=True)
            items = await self.repositories.inbox.list_for_cycle(snapshot.cycle_id)
            groups = defaultdict(list)
            for item in items:
                if (
                    item.state in {InboxState.CLAIMED, InboxState.APPLYING}
                    and item.claim_token
                    and item.claim_expires_at is not None
                    and item.claim_expires_at <= now
                ):
                    groups[item.claim_token].append(item)

            for group in groups.values():
                states = {item.state for item in group}
                claim = self._claim_from_group(group)
                if states == {InboxState.CLAIMED}:
                    await self.repositories.inbox.requeue_claim(
                        claim,
                        error_code="startup_claim_expired",
                    )
                    report.inbox_claims_reconciled += 1
                    continue
                if states != {InboxState.APPLYING}:
                    raise self._fatal("mixed_claim_apply_state")

                first = claim.first_cycle_sequence
                last = claim.last_cycle_sequence
                applied = snapshot.applied_through_cycle_sequence
                if first <= applied < last:
                    raise self._fatal("partial_claim_snapshot_authority")
                if last <= applied:
                    applied_ids = set(snapshot.applied_input_batch_ids)
                    if any(
                        item.input_batch_id not in applied_ids
                        for item in claim.items
                    ):
                        raise self._fatal("snapshot_applied_batch_identity_gap")
                    await self.repositories.inbox.mark_applied(
                        claim,
                        applied_at=now,
                    )
                    for item in claim.items:
                        admission = (
                            await self.repositories.admissions.get_by_input_batch_id(
                                item.input_batch_id
                            )
                        )
                        if admission is None:
                            raise self._fatal("inbox_admission_missing")
                        if admission.state == AdmissionState.ADMITTED:
                            await self.repositories.admissions.mark_applied(
                                admission.admission_id,
                                applied_at=now,
                            )
                    report.inbox_claims_reconciled += 1
                else:
                    await self.repositories.inbox.requeue_claim(
                        claim,
                        error_code="startup_apply_not_committed",
                    )
                    report.inbox_claims_reconciled += 1

            admissions = await self.repositories.admissions.list_for_session(
                snapshot.session_id
            )
            cycle_rows = sorted(
                (
                    item
                    for item in admissions
                    if item.target_cycle_id == snapshot.cycle_id
                    and item.admitted_generation == snapshot.generation
                ),
                key=lambda item: item.cycle_sequence,
            )
            by_sequence = {item.cycle_sequence: item for item in cycle_rows}
            expected = list(
                range(0, snapshot.applied_through_cycle_sequence + 1)
            )
            if any(sequence not in by_sequence for sequence in expected):
                raise self._fatal("snapshot_applied_sequence_gap")
            expected_ids = [
                by_sequence[sequence].input_batch_id for sequence in expected
            ]
            if snapshot.applied_input_batch_ids != expected_ids:
                raise self._fatal("snapshot_applied_batch_identity_mismatch")
            for sequence in expected:
                admission = by_sequence[sequence]
                if admission.state == AdmissionState.ADMITTED:
                    await self.repositories.admissions.mark_applied(
                        admission.admission_id,
                        applied_at=now,
                    )

            state = await self.repositories.sessions.get(snapshot.session_id)
            if state is None:
                raise self._fatal("snapshot_session_missing")
            await self.admission_service.recover_snapshot_session_authority(
                state=state,
                context_revision_id=snapshot.active_context_revision_id,
                applied_through=snapshot.applied_through_cycle_sequence,
                recovered_at=now,
            )

    async def _recover_controls(self, now, report) -> None:
        # A pause may have been durably accepted before the first safe snapshot
        # was ever persisted. Do not run a new semantic block merely to reach a
        # checkpoint: finish the command marker and retain the same cycle as an
        # explicit non-resumable interruption.
        states = await self.repositories.sessions.list_states()
        for state in states:
            if (
                state.cycle_status != CycleStatus.PAUSE_REQUESTED
                or state.active_cycle_id is None
                or state.pending_control_sequence <= state.applied_control_sequence
            ):
                continue
            snapshot = await self.repositories.snapshots.get(state.active_cycle_id)
            if snapshot is not None:
                continue
            rows = await self.repositories.controls.list_range(  # type: ignore[attr-defined]
                state.session_id,
                after_sequence=state.applied_control_sequence,
                through_sequence=state.pending_control_sequence,
            )
            expected = state.applied_control_sequence + 1
            if [item.sequence_number for item in rows] != list(
                range(expected, state.pending_control_sequence + 1)
            ):
                raise self._fatal("control_sequence_gap")
            actionable = [
                item
                for item in rows
                if item.state not in {
                    ControlState.APPLIED,
                    ControlState.REJECTED,
                    ControlState.CANCELLED,
                }
            ]
            if not actionable or any(
                item.command != ControlCommandType.PAUSE for item in actionable
            ):
                raise self._fatal("pause_without_snapshot_control_conflict")
            for item in actionable:
                current = await self.repositories.controls.apply(
                    item.control_id,
                    applied_at=now,
                )
                if current.state != ControlState.APPLIED:
                    raise self._fatal("pause_recovery_marker_not_applied")
            await self.admission_service.recover_control_applied_watermark(
                state.session_id
            )
            await self.admission_service.recover_cycle_status(
                session_id=state.session_id,
                cycle_id=state.active_cycle_id,
                generation=state.generation,
                status=CycleStatus.INTERRUPTED,
            )
            report.controls_reconciled += 1

        # Reuse the existing IR-5 reset/reducer semantics through explicit
        # public recovery commands; do not create a second control state machine.
        states = await self.repositories.sessions.list_states()
        for initial in states:
            rows = await self.repositories.controls.list_for_session(  # type: ignore[attr-defined]
                initial.session_id
            )
            for row in rows:
                if (
                    row.command == ControlCommandType.RESET
                    and row.state not in {
                        ControlState.APPLIED,
                        ControlState.REJECTED,
                        ControlState.CANCELLED,
                    }
                ):
                    await self.admission_service.recover_reset_command(row)
                    report.controls_reconciled += 1

            state = await self.repositories.sessions.get(initial.session_id)
            if (
                state is None
                or state.pending_control_sequence
                <= state.applied_control_sequence
            ):
                continue
            if state.active_cycle_id is None:
                raise self._fatal("pending_control_without_active_cycle")
            snapshot = await self.repositories.snapshots.get(state.active_cycle_id)
            if snapshot is None:
                raise self._fatal("pending_control_snapshot_missing")
            view = _RecoveryControlCycle(
                session_id=state.session_id,
                cycle_id=state.active_cycle_id,
                input_runtime_generation=state.generation,
            )
            outcome = await self.admission_service.control_service.reduce_at_checkpoint(
                checkpoint=CheckpointName.RESUME,
                active_cycle=view,
                through_control_sequence=state.pending_control_sequence,
            )
            if (
                outcome is not None
                and outcome.reason_code == "reset_generation_transition_pending"
            ):
                raise self._fatal("reset_generation_recovery_incomplete")
            report.controls_reconciled += 1

    async def _recover_finalizations(self, now, report) -> None:
        records = await self._finalizations_for_recovery()
        for record in records:
            if record.state == FinalizationState.FAILED_RECOVERABLE:
                await self._interrupt_if_current(
                    record,
                    reason_code=(
                        record.failure_code
                        or "startup_failed_finalization_requires_explicit_resume"
                    ),
                    now=now,
                )
            elif record.state == FinalizationState.OUTPUT_READY:
                await self._validate_output_ready_evidence(record)
        await super()._recover_finalizations(now, report)

    async def _build_session_plans(
        self,
        start_outcomes,
        now,
        report,
    ):
        plans: list[RecoverySessionPlan] = []
        states = await self.repositories.sessions.list_states()
        nonterminal_handoffs = {
            item.cycle_id: item
            for item in await self.repositories.handoffs.list_nonterminal_for_recovery()  # type: ignore[attr-defined]
        }
        for state in sorted(states, key=lambda item: item.session_id):
            cycle_id = state.active_cycle_id
            if cycle_id is None or state.cycle_status in {
                CycleStatus.IDLE,
                CycleStatus.DONE,
                CycleStatus.ERROR,
                CycleStatus.CANCELLED,
            }:
                continue
            snapshot = await self.repositories.snapshots.get(cycle_id)
            marker = nonterminal_handoffs.get(cycle_id)
            if snapshot is None:
                outcome = start_outcomes.get(cycle_id)
                if (
                    outcome is not None
                    and outcome.admission is not None
                    and outcome.admission.state == AdmissionState.ADMITTED
                    and await self.repositories.handoffs.get(
                        outcome.admission.admission_id
                    )
                    is None
                ):
                    plans.append(
                        RecoverySessionPlan(
                            session_id=state.session_id,
                            cycle_id=cycle_id,
                            generation=state.generation,
                            disposition=RecoveryDisposition.START_ADMITTED,
                            admission_outcome=outcome,
                        )
                    )
                    report.resumable_cycles += 1
                    continue
                if state.cycle_status == CycleStatus.INTERRUPTED:
                    disposition = (
                        RecoveryDisposition.AMBIGUOUS
                        if marker is not None
                        and marker.state == RuntimeHandoffState.AMBIGUOUS
                        else RecoveryDisposition.NON_RESUMABLE
                    )
                    plans.append(
                        RecoverySessionPlan(
                            session_id=state.session_id,
                            cycle_id=cycle_id,
                            generation=state.generation,
                            disposition=disposition,
                            reason_code=(
                                marker.error_code
                                if marker is not None
                                else "interrupted_without_safe_snapshot"
                            ),
                        )
                    )
                    continue
                raise self._fatal("active_cycle_snapshot_missing")

            try:
                await self._validate_snapshot(snapshot)
            except InputRuntimeRecoveryError as error:
                if error.fatal:
                    raise
                plans.append(
                    RecoverySessionPlan(
                        session_id=state.session_id,
                        cycle_id=cycle_id,
                        generation=state.generation,
                        disposition=RecoveryDisposition.NON_RESUMABLE,
                        snapshot=snapshot,
                        reason_code=error.reason_code,
                    )
                )
                continue

            if marker is not None and marker.state == RuntimeHandoffState.AMBIGUOUS:
                plans.append(
                    RecoverySessionPlan(
                        session_id=state.session_id,
                        cycle_id=cycle_id,
                        generation=state.generation,
                        disposition=RecoveryDisposition.AMBIGUOUS,
                        snapshot=snapshot,
                        reason_code=(
                            marker.error_code or "ambiguous_runtime_handoff"
                        ),
                    )
                )
                continue
            if state.cycle_status == CycleStatus.WAITING_USER:
                if (
                    snapshot.status != CycleStatus.WAITING_USER
                    or not snapshot.waiting_question
                ):
                    raise self._fatal("waiting_snapshot_authority_mismatch")
                plans.append(
                    RecoverySessionPlan(
                        state.session_id,
                        cycle_id,
                        state.generation,
                        RecoveryDisposition.WAITING,
                        snapshot,
                    )
                )
                report.waiting_cycles += 1
                report.resumable_cycles += 1
                continue
            if state.cycle_status == CycleStatus.PAUSED_BY_USER:
                if snapshot.status != CycleStatus.PAUSED_BY_USER:
                    raise self._fatal("paused_snapshot_authority_mismatch")
                plans.append(
                    RecoverySessionPlan(
                        state.session_id,
                        cycle_id,
                        state.generation,
                        RecoveryDisposition.PAUSED,
                        snapshot,
                    )
                )
                report.paused_cycles += 1
                report.resumable_cycles += 1
                continue
            if state.cycle_status == CycleStatus.PAUSE_REQUESTED:
                raise self._fatal("pause_requested_not_reconciled")
            if state.cycle_status == CycleStatus.FINALIZING:
                raise self._fatal("finalizing_state_not_reconciled")
            if state.cycle_status == CycleStatus.RUNNING:
                snapshot = await self.repositories.snapshots.mark_recovery_interrupted(  # type: ignore[attr-defined]
                    session_id=state.session_id,
                    cycle_id=cycle_id,
                    generation=state.generation,
                    reason_code="startup_safe_restart",
                    interrupted_at=now,
                )
                plans.append(
                    RecoverySessionPlan(
                        state.session_id,
                        cycle_id,
                        state.generation,
                        RecoveryDisposition.AUTO_RESUME_SAFE,
                        snapshot,
                        reason_code="startup_safe_restart",
                    )
                )
                report.resumable_cycles += 1
                continue
            if state.cycle_status == CycleStatus.INTERRUPTED:
                plans.append(
                    RecoverySessionPlan(
                        state.session_id,
                        cycle_id,
                        state.generation,
                        RecoveryDisposition.INTERRUPTED,
                        snapshot,
                        reason_code=snapshot.interruption_reason,
                    )
                )
                report.resumable_cycles += 1
                continue
            raise self._fatal("unsupported_recovery_cycle_status")
        return tuple(plans)

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
