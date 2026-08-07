"""IR-5 filesystem control adapter extensions.

The application layer uses command-oriented methods only; these audit reads are
also useful to deterministic tests and future diagnostics without exposing file
paths or layout.
"""
from __future__ import annotations

from ._filesystem_common import validated_copy
from ._filesystem_identity_recovery_common import recover_cycle_authority
from . import _filesystem_identity_recovery_session as _session_control_module
from ._filesystem_session import _same_control_relation
from .errors import InputRuntimeConflictError, InputRuntimeNotFoundError
from .models import (
    ControlState,
    CycleStatus,
    SessionControlCommand,
    SessionInputRuntimeState,
)
from .serialization import read_model


_BaseControlRepository = _session_control_module.FileSystemSessionControlRepository


class FileSystemSessionControlRepository(_BaseControlRepository):
    async def get(self, control_id: str) -> SessionControlCommand | None:
        return self._recover_by_id(control_id)

    async def list_for_session(
        self,
        session_id: str,
    ) -> tuple[SessionControlCommand, ...]:
        return await self._all(session_id)

    def _restore_indexes(self, command: SessionControlCommand) -> None:
        # Rejected no-op pause/continue records may carry a synthetic target to
        # satisfy the IR-1 schema. They are audit evidence, never cycle authority.
        if (
            command.target_cycle_id is not None
            and command.state != ControlState.REJECTED
        ):
            recover_cycle_authority(
                self,
                command.target_cycle_id,
                command.session_id,
            )
        self._index(command)
        if (
            command.target_cycle_id is not None
            and command.state != ControlState.REJECTED
        ):
            self._ensure_cycle_authority(
                command.target_cycle_id,
                command.session_id,
            )

    async def _repair_control_frontier_locked(
        self,
        state_path,
        state: SessionInputRuntimeState,
    ) -> SessionInputRuntimeState:
        """Repair pending sequence from exact-session durable control records.

        This is intentionally bounded to ``sessions/<session>/controls`` and is
        executed under the existing root-identity -> session coordination lock.
        A record-first publication may therefore be followed by an unrelated
        command without reusing the durable sequence whose state write failed.
        """
        rows = await self._all(state.session_id)
        by_sequence: dict[int, str] = {}
        frontier = state.pending_control_sequence
        frontier_time = state.updated_at
        for row in rows:
            previous = by_sequence.get(row.sequence_number)
            if previous is not None and previous != row.control_id:
                raise InputRuntimeConflictError(
                    "duplicate durable control sequence for session"
                )
            by_sequence[row.sequence_number] = row.control_id
            if row.sequence_number > frontier:
                frontier = row.sequence_number
            if row.created_at > frontier_time:
                frontier_time = row.created_at
        if frontier <= state.pending_control_sequence:
            return state
        repaired = validated_copy(
            state,
            pending_control_sequence=frontier,
            revision=state.revision + 1,
            updated_at=frontier_time,
        )
        _session_control_module.atomic_write_model(state_path, repaired)
        return repaired

    async def allocate(
        self,
        command: SessionControlCommand,
    ) -> SessionControlCommand:
        """Allocate after repairing the authoritative exact-session frontier."""
        async with self.locks.hold_identity_then_session(
            self.root,
            command.session_id,
        ):
            state_path = self.layout.state(command.session_id)
            if not state_path.exists():
                raise InputRuntimeNotFoundError(
                    "session runtime state required for control allocation"
                )
            state = read_model(state_path, SessionInputRuntimeState)
            state = await self._repair_control_frontier_locked(state_path, state)

            existing = await self.get_by_idempotency_key(
                command.session_id,
                command.idempotency_key,
            )
            if existing is not None:
                if not _same_control_relation(existing, command):
                    raise InputRuntimeConflictError(
                        "control idempotency relation changed"
                    )
                self._restore_indexes(existing)
                await self._repair_existing_pending(state_path, state, existing)
                return existing
            if command.generation != state.generation:
                raise InputRuntimeConflictError("control generation changed")

            allocated = validated_copy(
                command,
                sequence_number=state.pending_control_sequence + 1,
            )
            by_id = self._recover_by_id(allocated.control_id)
            if by_id is not None:
                if by_id != allocated:
                    raise InputRuntimeConflictError(
                        "control stable ID collision"
                    )
                self._restore_indexes(by_id)
                await self._repair_existing_pending(state_path, state, by_id)
                return by_id

            if (
                allocated.target_cycle_id is not None
                and allocated.state != ControlState.REJECTED
            ):
                recover_cycle_authority(
                    self,
                    allocated.target_cycle_id,
                    allocated.session_id,
                )
            _session_control_module.atomic_write_model(
                self.layout.control(
                    allocated.session_id,
                    allocated.control_id,
                ),
                allocated,
            )
            self._index(allocated)
            if (
                allocated.target_cycle_id is not None
                and allocated.state != ControlState.REJECTED
            ):
                self._ensure_cycle_authority(
                    allocated.target_cycle_id,
                    allocated.session_id,
                )
            _session_control_module.atomic_write_model(
                state_path,
                self._state_after_control(state, allocated),
            )
            return allocated

    async def accept_reset(
        self,
        command: SessionControlCommand,
    ) -> tuple[SessionControlCommand, SessionInputRuntimeState]:
        """Publish reset after exact-session frontier repair, then advance generation."""
        async with self.locks.hold_identity_then_session(
            self.root,
            command.session_id,
        ):
            state_path = self.layout.state(command.session_id)
            if not state_path.exists():
                raise InputRuntimeNotFoundError(
                    "session runtime state required for reset"
                )
            state = read_model(state_path, SessionInputRuntimeState)
            state = await self._repair_control_frontier_locked(state_path, state)

            existing = await self.get_by_idempotency_key(
                command.session_id,
                command.idempotency_key,
            )
            if existing is not None:
                if not _same_control_relation(existing, command):
                    raise InputRuntimeConflictError(
                        "control idempotency relation changed"
                    )
                self._restore_indexes(existing)
                if state.generation == existing.generation:
                    next_state = validated_copy(
                        state,
                        generation=state.generation + 1,
                        active_cycle_id=None,
                        cycle_status=CycleStatus.IDLE,
                        active_cycle_accepted_through_sequence=0,
                        active_cycle_applied_through_sequence=0,
                        active_context_revision_id=None,
                        finalization_id=None,
                        pending_control_sequence=max(
                            state.pending_control_sequence,
                            existing.sequence_number,
                        ),
                        revision=state.revision + 1,
                        updated_at=max(state.updated_at, existing.created_at),
                    )
                    _session_control_module.atomic_write_model(state_path, next_state)
                    return existing, next_state
                if state.generation >= existing.generation + 1:
                    repaired = await self._repair_existing_pending(
                        state_path,
                        state,
                        existing,
                    )
                    return existing, repaired
                raise InputRuntimeConflictError("reset generation diverged")

            if command.generation != state.generation:
                raise InputRuntimeConflictError("reset generation changed")
            allocated = validated_copy(
                command,
                sequence_number=state.pending_control_sequence + 1,
            )
            by_id = self._recover_by_id(allocated.control_id)
            if by_id is not None:
                if by_id != allocated:
                    raise InputRuntimeConflictError(
                        "control stable ID collision"
                    )
                self._restore_indexes(by_id)
                raise InputRuntimeConflictError(
                    "reset record exists without idempotency relation"
                )
            if allocated.target_cycle_id is not None:
                recover_cycle_authority(
                    self,
                    allocated.target_cycle_id,
                    allocated.session_id,
                )
            _session_control_module.atomic_write_model(
                self.layout.control(
                    allocated.session_id,
                    allocated.control_id,
                ),
                allocated,
            )
            self._index(allocated)
            if allocated.target_cycle_id is not None:
                self._ensure_cycle_authority(
                    allocated.target_cycle_id,
                    allocated.session_id,
                )
            next_state = validated_copy(
                state,
                generation=state.generation + 1,
                active_cycle_id=None,
                cycle_status=CycleStatus.IDLE,
                active_cycle_accepted_through_sequence=0,
                active_cycle_applied_through_sequence=0,
                active_context_revision_id=None,
                finalization_id=None,
                pending_control_sequence=allocated.sequence_number,
                revision=state.revision + 1,
                updated_at=max(state.updated_at, allocated.created_at),
            )
            _session_control_module.atomic_write_model(state_path, next_state)
            return allocated, next_state
