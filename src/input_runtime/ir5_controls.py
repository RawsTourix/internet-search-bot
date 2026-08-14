"""IR-5 transport-neutral durable control-plane service.

The service owns semantic control decisions while repositories own short durable
coordination primitives.  It deliberately has no filesystem or transport
imports so the same contract can be backed by PostgreSQL in v0.5.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from .errors import InputRuntimeConflictError
from .factory import InputRuntimeRepositories
from .models import (
    ActiveCycleSnapshot,
    CheckpointAction,
    CheckpointName,
    CheckpointOutcome,
    ControlCommandType,
    ControlOutcome,
    ControlState,
    CycleStatus,
    SessionControlCommand,
    SessionInputRuntimeState,
    new_control_id,
)


class ControlWakeCoordinator(Protocol):
    async def wake(self, session_id: str, *, cycle_id: str) -> bool: ...


class DurableControlRepository(Protocol):
    async def allocate(self, command: SessionControlCommand) -> SessionControlCommand: ...
    async def accept_reset(
        self,
        command: SessionControlCommand,
    ) -> tuple[SessionControlCommand, SessionInputRuntimeState]: ...
    async def list_range(
        self,
        session_id: str,
        *,
        after_sequence: int,
        through_sequence: int,
    ) -> tuple[SessionControlCommand, ...]: ...
    async def cancel_generation_except(
        self,
        session_id: str,
        *,
        generation: int,
        reason_code: str,
        exclude_control_ids: tuple[str, ...] = (),
    ) -> tuple[SessionControlCommand, ...]: ...


_TERMINAL_OR_IDLE = {
    CycleStatus.IDLE,
    CycleStatus.DONE,
    CycleStatus.ERROR,
    CycleStatus.CANCELLED,
}
_TERMINAL_CONTROL_STATES = {
    ControlState.APPLIED,
    ControlState.REJECTED,
    ControlState.CANCELLED,
}


class InputRuntimeControlService:
    """Accept, repair and reduce durable runtime controls for one session."""

    def __init__(
        self,
        *,
        repositories: InputRuntimeRepositories,
        wake_coordinator: ControlWakeCoordinator | None = None,
        clock: Callable[[], datetime] | None = None,
        control_id_factory: Callable[[], str] = new_control_id,
    ) -> None:
        self.repositories = repositories
        self.wake_coordinator = wake_coordinator
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.control_id_factory = control_id_factory
        controls = repositories.controls
        for method in ("allocate", "accept_reset", "list_range", "cancel_generation_except"):
            if not callable(getattr(controls, method, None)):
                raise TypeError(f"IR-5 control repository missing {method}()")

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("control clock must return timezone-aware datetime")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _inactive_target(session_id: str) -> str:
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:20]
        return f"inactive-control-target:{digest}"

    @staticmethod
    def _source_ref(
        source_message_ref: dict[str, Any] | None,
        *,
        accepted_input_through_sequence: int | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {"source": source_message_ref or {}}
        if accepted_input_through_sequence is not None:
            result["runtime"] = {
                "accepted_input_through_sequence": accepted_input_through_sequence,
            }
        return result

    @staticmethod
    def resume_input_target(command: SessionControlCommand) -> int | None:
        if command.command != ControlCommandType.CONTINUE:
            return None
        ref = command.source_message_ref or {}
        runtime = ref.get("runtime")
        if not isinstance(runtime, dict):
            return None
        value = runtime.get("accepted_input_through_sequence")
        return value if isinstance(value, int) and value >= 0 else None

    @staticmethod
    def _external_source_ref(command: SessionControlCommand) -> dict[str, Any]:
        ref = command.source_message_ref or {}
        source = ref.get("source")
        return source if isinstance(source, dict) else {}

    async def _state(self, session_id: str) -> SessionInputRuntimeState:
        current = await self.repositories.sessions.get(session_id)
        if current is not None:
            return current
        now = self._now()
        return await self.repositories.sessions.create_if_absent(
            SessionInputRuntimeState(
                session_id=session_id,
                generation=0,
                created_at=now,
                updated_at=now,
            )
        )

    @staticmethod
    def _same_delivery(
        existing: SessionControlCommand,
        *,
        command: ControlCommandType,
        source_client_type: str,
        source_message_ref: dict[str, Any] | None,
    ) -> bool:
        return (
            existing.command == command
            and existing.source_client_type == source_client_type.strip()
            and InputRuntimeControlService._external_source_ref(existing)
            == (source_message_ref or {})
        )

    async def _existing(
        self,
        *,
        session_id: str,
        idempotency_key: str,
        command: ControlCommandType,
        source_client_type: str,
        source_message_ref: dict[str, Any] | None,
    ) -> SessionControlCommand | None:
        existing = await self.repositories.controls.get_by_idempotency_key(
            session_id,
            idempotency_key,
        )
        if existing is None:
            return None
        if not self._same_delivery(
            existing,
            command=command,
            source_client_type=source_client_type,
            source_message_ref=source_message_ref,
        ):
            raise InputRuntimeConflictError(
                "control idempotency key was reused for a different command delivery"
            )
        return existing

    async def _advance_applied_watermark(self, session_id: str) -> SessionInputRuntimeState:
        controls: DurableControlRepository = self.repositories.controls  # type: ignore[assignment]
        for _ in range(12):
            state = await self._state(session_id)
            if state.applied_control_sequence >= state.pending_control_sequence:
                return state
            rows = await controls.list_range(
                session_id,
                after_sequence=state.applied_control_sequence,
                through_sequence=state.pending_control_sequence,
            )
            expected = state.applied_control_sequence + 1
            contiguous = state.applied_control_sequence
            for row in rows:
                if row.sequence_number != expected:
                    break
                if row.state not in _TERMINAL_CONTROL_STATES:
                    break
                contiguous = row.sequence_number
                expected += 1
            if contiguous == state.applied_control_sequence:
                return state
            candidate = state.model_copy(
                update={
                    "applied_control_sequence": contiguous,
                    "revision": state.revision + 1,
                    "updated_at": self._now(),
                }
            )
            try:
                return await self.repositories.sessions.compare_and_swap(
                    state.revision,
                    candidate,
                )
            except InputRuntimeConflictError:
                continue
        raise InputRuntimeConflictError("control applied watermark CAS exhausted")

    async def _set_cycle_status(
        self,
        *,
        session_id: str,
        cycle_id: str,
        generation: int,
        status: CycleStatus,
    ) -> SessionInputRuntimeState:
        for _ in range(12):
            state = await self._state(session_id)
            if state.generation != generation or state.active_cycle_id != cycle_id:
                raise InputRuntimeConflictError("control lost active cycle authority")
            if state.cycle_status == status:
                return state
            candidate = state.model_copy(
                update={
                    "cycle_status": status,
                    "finalization_id": (
                        state.finalization_id
                        if status == CycleStatus.FINALIZING
                        else None
                    ),
                    "revision": state.revision + 1,
                    "updated_at": self._now(),
                }
            )
            try:
                return await self.repositories.sessions.compare_and_swap(
                    state.revision,
                    candidate,
                )
            except InputRuntimeConflictError:
                continue
        raise InputRuntimeConflictError("control status CAS exhausted")

    async def _persist_snapshot_status(
        self,
        *,
        session_id: str,
        cycle_id: str,
        generation: int,
        status: CycleStatus,
        checkpoint: CheckpointName,
        pause_reason: str | None = None,
    ) -> ActiveCycleSnapshot:
        for _ in range(12):
            state = await self._state(session_id)
            snapshot = await self.repositories.snapshots.get(cycle_id)
            if (
                snapshot is None
                or state.generation != generation
                or state.active_cycle_id != cycle_id
                or snapshot.session_id != session_id
                or snapshot.generation != generation
            ):
                raise InputRuntimeConflictError("control snapshot authority mismatch")
            desired_waiting = snapshot.waiting_question
            desired_interruption = snapshot.interruption_reason
            updates: dict[str, Any] = {
                "status": status,
                "safe_checkpoint": checkpoint,
                "snapshot_revision": snapshot.snapshot_revision + 1,
                "updated_at": self._now(),
            }
            if status == CycleStatus.PAUSED_BY_USER:
                updates["pause_reason"] = pause_reason or "user_pause"
            else:
                updates["pause_reason"] = None
            if status == CycleStatus.RUNNING:
                updates["waiting_question"] = None
                updates["interruption_reason"] = None
            elif status == CycleStatus.WAITING_USER:
                if not desired_waiting:
                    raise InputRuntimeConflictError(
                        "cannot restore waiting state without durable question"
                    )
                updates["waiting_question"] = desired_waiting
                updates["interruption_reason"] = None
            else:
                updates["waiting_question"] = desired_waiting
                updates["interruption_reason"] = desired_interruption
            if (
                snapshot.status == status
                and snapshot.pause_reason == updates["pause_reason"]
                and snapshot.waiting_question == updates.get("waiting_question")
                and snapshot.interruption_reason == updates.get("interruption_reason")
            ):
                persisted = snapshot
            else:
                candidate = ActiveCycleSnapshot.model_validate(
                    snapshot.model_copy(update=updates).model_dump(mode="python")
                )
                try:
                    persisted = await self.repositories.snapshots.compare_and_swap(
                        snapshot.snapshot_revision,
                        candidate,
                    )
                except InputRuntimeConflictError:
                    continue
            await self._set_cycle_status(
                session_id=session_id,
                cycle_id=cycle_id,
                generation=generation,
                status=status,
            )
            return persisted
        raise InputRuntimeConflictError("control snapshot CAS exhausted")

    async def _apply_pause_command(
        self,
        command: SessionControlCommand,
        *,
        checkpoint: CheckpointName,
    ) -> SessionControlCommand:
        assert command.target_cycle_id is not None
        await self._persist_snapshot_status(
            session_id=command.session_id,
            cycle_id=command.target_cycle_id,
            generation=command.generation,
            status=CycleStatus.PAUSED_BY_USER,
            checkpoint=checkpoint,
            pause_reason=command.reason or "user_pause",
        )
        current = await self.repositories.controls.apply(
            command.control_id,
            applied_at=self._now(),
        )
        await self._advance_applied_watermark(command.session_id)
        return current

    async def _apply_resume_command(
        self,
        command: SessionControlCommand,
        *,
        checkpoint: CheckpointName,
    ) -> tuple[SessionControlCommand, CycleStatus]:
        assert command.target_cycle_id is not None
        snapshot = await self.repositories.snapshots.get(command.target_cycle_id)
        if snapshot is None:
            raise InputRuntimeConflictError("resume snapshot unavailable")
        target = (
            CycleStatus.WAITING_USER
            if snapshot.waiting_question
            else CycleStatus.RUNNING
        )
        await self._persist_snapshot_status(
            session_id=command.session_id,
            cycle_id=command.target_cycle_id,
            generation=command.generation,
            status=target,
            checkpoint=checkpoint,
        )
        current = await self.repositories.controls.apply(
            command.control_id,
            applied_at=self._now(),
        )
        await self._advance_applied_watermark(command.session_id)
        return current, target

    async def request_pause(
        self,
        *,
        session_id: str,
        idempotency_key: str,
        source_client_type: str,
        source_message_ref: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> ControlOutcome:
        existing = await self._existing(
            session_id=session_id,
            idempotency_key=idempotency_key,
            command=ControlCommandType.PAUSE,
            source_client_type=source_client_type,
            source_message_ref=source_message_ref,
        )
        if existing is not None:
            if existing.state == ControlState.QUEUED:
                return await self._repair_pause(existing)
            state = await self._state(session_id)
            return ControlOutcome(
                outcome=existing.state,
                command=existing,
                effective_cycle_status=state.cycle_status,
            )
        state = await self._state(session_id)
        if state.cycle_status in _TERMINAL_OR_IDLE or state.active_cycle_id is None:
            command = SessionControlCommand(
                control_id=self.control_id_factory(),
                session_id=session_id,
                target_cycle_id=self._inactive_target(session_id),
                generation=state.generation,
                sequence_number=1,
                command=ControlCommandType.PAUSE,
                state=ControlState.REJECTED,
                idempotency_key=idempotency_key,
                source_client_type=source_client_type,
                source_message_ref=self._source_ref(source_message_ref),
                reason=reason,
                created_at=self._now(),
                rejection_code="no_active_cycle",
            )
            allocated = await self.repositories.controls.allocate(command)  # type: ignore[attr-defined]
            await self._advance_applied_watermark(session_id)
            return ControlOutcome(
                outcome=allocated.state,
                command=allocated,
                effective_cycle_status=state.cycle_status,
            )
        rejection = None
        if state.cycle_status == CycleStatus.PAUSED_BY_USER:
            rejection = "already_paused"
        elif state.cycle_status == CycleStatus.PAUSE_REQUESTED:
            rejection = "pause_pending"
        command = SessionControlCommand(
            control_id=self.control_id_factory(),
            session_id=session_id,
            target_cycle_id=state.active_cycle_id,
            generation=state.generation,
            sequence_number=1,
            command=ControlCommandType.PAUSE,
            state=ControlState.REJECTED if rejection else ControlState.QUEUED,
            idempotency_key=idempotency_key,
            source_client_type=source_client_type,
            source_message_ref=self._source_ref(source_message_ref),
            reason=reason,
            created_at=self._now(),
            rejection_code=rejection,
        )
        allocated = await self.repositories.controls.allocate(command)  # type: ignore[attr-defined]
        if allocated.state == ControlState.REJECTED:
            await self._advance_applied_watermark(session_id)
            return ControlOutcome(
                outcome=allocated.state,
                command=allocated,
                effective_cycle_status=state.cycle_status,
            )
        return await self._repair_pause(allocated)

    async def _repair_pause(self, command: SessionControlCommand) -> ControlOutcome:
        state = await self._state(command.session_id)
        if state.generation != command.generation or state.active_cycle_id != command.target_cycle_id:
            current = await self.repositories.controls.reject(
                command.control_id,
                rejection_code="stale_cycle",
            )
            await self._advance_applied_watermark(command.session_id)
            return ControlOutcome(
                outcome=current.state,
                command=current,
                effective_cycle_status=state.cycle_status,
            )
        if state.cycle_status in {CycleStatus.WAITING_USER, CycleStatus.INTERRUPTED}:
            applied = await self._apply_pause_command(
                command,
                checkpoint=CheckpointName.RESUME,
            )
            return ControlOutcome(
                outcome=applied.state,
                command=applied,
                effective_cycle_status=CycleStatus.PAUSED_BY_USER,
            )
        if state.cycle_status == CycleStatus.PAUSED_BY_USER:
            applied = await self._apply_pause_command(
                command,
                checkpoint=CheckpointName.RESUME,
            )
            return ControlOutcome(
                outcome=applied.state,
                command=applied,
                effective_cycle_status=CycleStatus.PAUSED_BY_USER,
            )
        if state.cycle_status not in {CycleStatus.RUNNING, CycleStatus.FINALIZING, CycleStatus.PAUSE_REQUESTED}:
            current = await self.repositories.controls.reject(
                command.control_id,
                rejection_code="no_active_cycle",
            )
            await self._advance_applied_watermark(command.session_id)
            return ControlOutcome(
                outcome=current.state,
                command=current,
                effective_cycle_status=state.cycle_status,
            )
        await self._set_cycle_status(
            session_id=command.session_id,
            cycle_id=command.target_cycle_id or "",
            generation=command.generation,
            status=CycleStatus.PAUSE_REQUESTED,
        )
        current = await self.repositories.controls.acknowledge(
            command.control_id,
            acknowledged_at=self._now(),
        )
        if self.wake_coordinator is not None and command.target_cycle_id is not None:
            await self.wake_coordinator.wake(
                command.session_id,
                cycle_id=command.target_cycle_id,
            )
        return ControlOutcome(
            outcome=current.state,
            command=current,
            effective_cycle_status=CycleStatus.PAUSE_REQUESTED,
        )

    async def request_continue(
        self,
        *,
        session_id: str,
        idempotency_key: str,
        source_client_type: str,
        source_message_ref: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> ControlOutcome:
        existing = await self._existing(
            session_id=session_id,
            idempotency_key=idempotency_key,
            command=ControlCommandType.CONTINUE,
            source_client_type=source_client_type,
            source_message_ref=source_message_ref,
        )
        if existing is not None:
            if existing.state == ControlState.QUEUED:
                return await self._repair_continue(existing)
            state = await self._state(session_id)
            return ControlOutcome(
                outcome=existing.state,
                command=existing,
                effective_cycle_status=state.cycle_status,
            )
        state = await self._state(session_id)
        rejection = None
        if state.cycle_status in _TERMINAL_OR_IDLE or state.active_cycle_id is None:
            rejection = "nothing_to_continue"
        elif state.cycle_status in {CycleStatus.RUNNING, CycleStatus.FINALIZING}:
            rejection = "already_running"
        elif state.cycle_status == CycleStatus.WAITING_USER:
            rejection = "still_waiting_for_input"
        target = state.active_cycle_id or self._inactive_target(session_id)
        command = SessionControlCommand(
            control_id=self.control_id_factory(),
            session_id=session_id,
            target_cycle_id=target,
            generation=state.generation,
            sequence_number=1,
            command=ControlCommandType.CONTINUE,
            state=ControlState.REJECTED if rejection else ControlState.QUEUED,
            idempotency_key=idempotency_key,
            source_client_type=source_client_type,
            source_message_ref=self._source_ref(
                source_message_ref,
                accepted_input_through_sequence=(
                    state.active_cycle_accepted_through_sequence
                    if rejection is None
                    else None
                ),
            ),
            reason=reason,
            created_at=self._now(),
            rejection_code=rejection,
        )
        allocated = await self.repositories.controls.allocate(command)  # type: ignore[attr-defined]
        if allocated.state == ControlState.REJECTED:
            await self._advance_applied_watermark(session_id)
            return ControlOutcome(
                outcome=allocated.state,
                command=allocated,
                effective_cycle_status=state.cycle_status,
            )
        return await self._repair_continue(allocated)

    async def _repair_continue(self, command: SessionControlCommand) -> ControlOutcome:
        state = await self._state(command.session_id)
        if state.generation != command.generation or state.active_cycle_id != command.target_cycle_id:
            current = await self.repositories.controls.reject(
                command.control_id,
                rejection_code="stale_cycle",
            )
            await self._advance_applied_watermark(command.session_id)
            return ControlOutcome(
                outcome=current.state,
                command=current,
                effective_cycle_status=state.cycle_status,
            )
        if state.cycle_status == CycleStatus.PAUSE_REQUESTED:
            await self._set_cycle_status(
                session_id=command.session_id,
                cycle_id=command.target_cycle_id or "",
                generation=command.generation,
                status=CycleStatus.RUNNING,
            )
        current = await self.repositories.controls.acknowledge(
            command.control_id,
            acknowledged_at=self._now(),
        )
        return ControlOutcome(
            outcome=current.state,
            command=current,
            effective_cycle_status=(
                CycleStatus.RUNNING
                if state.cycle_status == CycleStatus.PAUSE_REQUESTED
                else state.cycle_status
            ),
        )

    async def request_reset(
        self,
        *,
        session_id: str,
        idempotency_key: str,
        source_client_type: str,
        source_message_ref: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> ControlOutcome:
        existing = await self._existing(
            session_id=session_id,
            idempotency_key=idempotency_key,
            command=ControlCommandType.RESET,
            source_client_type=source_client_type,
            source_message_ref=source_message_ref,
        )
        if existing is not None:
            return await self._reconcile_reset(existing)
        controls: DurableControlRepository = self.repositories.controls  # type: ignore[assignment]
        for _ in range(12):
            state = await self._state(session_id)
            command = SessionControlCommand(
                control_id=self.control_id_factory(),
                session_id=session_id,
                target_cycle_id=state.active_cycle_id,
                generation=state.generation,
                sequence_number=1,
                command=ControlCommandType.RESET,
                state=ControlState.QUEUED,
                idempotency_key=idempotency_key,
                source_client_type=source_client_type,
                source_message_ref=self._source_ref(source_message_ref),
                reason=reason,
                created_at=self._now(),
            )
            try:
                allocated, _ = await controls.accept_reset(command)
            except InputRuntimeConflictError:
                duplicate = await self._existing(
                    session_id=session_id,
                    idempotency_key=idempotency_key,
                    command=ControlCommandType.RESET,
                    source_client_type=source_client_type,
                    source_message_ref=source_message_ref,
                )
                if duplicate is not None:
                    return await self._reconcile_reset(duplicate)
                continue
            return await self._reconcile_reset(allocated)
        raise InputRuntimeConflictError("reset acceptance CAS exhausted")

    async def _reconcile_reset(self, command: SessionControlCommand) -> ControlOutcome:
        controls: DurableControlRepository = self.repositories.controls  # type: ignore[assignment]
        current, state = await controls.accept_reset(command)
        old_generation = command.generation
        reason = "session_reset"
        await self.repositories.admissions.cancel_generation(
            command.session_id,
            generation=old_generation,
            reason_code=reason,
        )
        await self.repositories.inbox.cancel_generation(
            command.session_id,
            generation=old_generation,
            reason_code=reason,
        )
        await controls.cancel_generation_except(
            command.session_id,
            generation=old_generation,
            reason_code=reason,
            exclude_control_ids=(command.control_id,),
        )
        await self.repositories.snapshots.cancel_generation(
            command.session_id,
            generation=old_generation,
            reason_code=reason,
        )
        await self.repositories.emissions.cancel_generation(
            command.session_id,
            generation=old_generation,
            reason_code=reason,
        )
        await self.repositories.finalizations.cancel_generation(
            command.session_id,
            generation=old_generation,
            reason_code=reason,
        )
        if current.state != ControlState.APPLIED:
            current = await self.repositories.controls.apply(
                command.control_id,
                applied_at=self._now(),
            )
        await self._advance_applied_watermark(command.session_id)
        coordinator = self.wake_coordinator
        if coordinator is not None:
            synchronize = getattr(coordinator, "synchronize_generation", None)
            if callable(synchronize):
                await synchronize(command.session_id, generation=state.generation)
        return ControlOutcome(
            outcome=current.state,
            command=current,
            effective_cycle_status=CycleStatus.IDLE,
        )

    async def reduce_at_checkpoint(
        self,
        *,
        checkpoint: CheckpointName,
        active_cycle: Any,
        through_control_sequence: int,
    ) -> CheckpointOutcome | None:
        session_id = str(active_cycle.session_id)
        cycle_id = str(active_cycle.cycle_id)
        generation = int(getattr(active_cycle, "input_runtime_generation", 0))
        state = await self._state(session_id)
        if state.generation != generation or state.active_cycle_id != cycle_id:
            return CheckpointOutcome(
                checkpoint=checkpoint,
                action=CheckpointAction.INTERRUPT,
                reason_code="checkpoint_runner_generation_stale",
            )
        if through_control_sequence <= state.applied_control_sequence:
            return None
        controls: DurableControlRepository = self.repositories.controls  # type: ignore[assignment]
        rows = await controls.list_range(
            session_id,
            after_sequence=state.applied_control_sequence,
            through_sequence=through_control_sequence,
        )
        expected = state.applied_control_sequence + 1
        if not rows or rows[0].sequence_number != expected:
            return CheckpointOutcome(
                checkpoint=checkpoint,
                action=CheckpointAction.INTERRUPT,
                reason_code="control_sequence_gap",
            )
        snapshot = await self.repositories.snapshots.get(cycle_id)
        if snapshot is None or snapshot.generation != generation:
            return CheckpointOutcome(
                checkpoint=checkpoint,
                action=CheckpointAction.INTERRUPT,
                reason_code="control_snapshot_authority_mismatch",
            )
        initial_status = snapshot.status
        desired_status = initial_status
        pause_command: SessionControlCommand | None = None
        actionable: list[SessionControlCommand] = []
        for row in rows:
            if row.sequence_number != expected:
                return CheckpointOutcome(
                    checkpoint=checkpoint,
                    action=CheckpointAction.INTERRUPT,
                    reason_code="control_sequence_gap",
                )
            expected += 1
            if row.state in _TERMINAL_CONTROL_STATES:
                continue
            if row.generation != generation:
                continue
            actionable.append(row)
            if row.command == ControlCommandType.RESET:
                return CheckpointOutcome(
                    checkpoint=checkpoint,
                    action=CheckpointAction.INTERRUPT,
                    reason_code="reset_generation_transition_pending",
                )
            if row.command == ControlCommandType.PAUSE:
                pause_command = row
                desired_status = CycleStatus.PAUSED_BY_USER
            elif row.command == ControlCommandType.CONTINUE:
                if desired_status == CycleStatus.PAUSED_BY_USER:
                    if initial_status == CycleStatus.PAUSED_BY_USER:
                        desired_status = (
                            CycleStatus.WAITING_USER
                            if snapshot.waiting_question
                            else CycleStatus.RUNNING
                        )
                    else:
                        desired_status = initial_status
                    pause_command = None
        if desired_status == CycleStatus.PAUSED_BY_USER:
            await self._persist_snapshot_status(
                session_id=session_id,
                cycle_id=cycle_id,
                generation=generation,
                status=CycleStatus.PAUSED_BY_USER,
                checkpoint=checkpoint,
                pause_reason=(pause_command.reason if pause_command else None),
            )
        elif desired_status in {CycleStatus.RUNNING, CycleStatus.WAITING_USER} and (
            initial_status == CycleStatus.PAUSED_BY_USER
            or state.cycle_status == CycleStatus.PAUSE_REQUESTED
        ):
            await self._persist_snapshot_status(
                session_id=session_id,
                cycle_id=cycle_id,
                generation=generation,
                status=desired_status,
                checkpoint=checkpoint,
            )
        for row in actionable:
            current = await self.repositories.controls.apply(
                row.control_id,
                applied_at=self._now(),
            )
            if current.state != ControlState.APPLIED:
                raise InputRuntimeConflictError("control apply did not persist")
        await self._advance_applied_watermark(session_id)
        if desired_status == CycleStatus.PAUSED_BY_USER:
            return CheckpointOutcome(
                checkpoint=checkpoint,
                action=CheckpointAction.PAUSE,
                context_revision_id=snapshot.active_context_revision_id,
                applied_through_cycle_sequence=snapshot.applied_through_cycle_sequence,
                reason_code="paused_by_user",
            )
        if desired_status == CycleStatus.WAITING_USER:
            return CheckpointOutcome(
                checkpoint=checkpoint,
                action=CheckpointAction.WAIT,
                context_revision_id=snapshot.active_context_revision_id,
                applied_through_cycle_sequence=snapshot.applied_through_cycle_sequence,
                reason_code="still_waiting_for_input",
            )
        return None
