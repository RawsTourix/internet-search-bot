"""Filesystem command adapter for the IR-7 finalization barrier."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from ._filesystem_common import validated_copy
from ._filesystem_delivery import _final_identity
from ._filesystem_identity_recovery_delivery import (
    FileSystemFinalizationRepository as _IR2FinalizationRepository,
)
from .errors import InputRuntimeConflictError, InputRuntimeNotFoundError
from .models import (
    ActiveCycleSnapshot,
    CycleFinalizationRecord,
    CycleStatus,
    FinalizationState,
    SessionInputRuntimeState,
)
from .serialization import atomic_write_model, list_models, read_model, storage_key


class _FinalResultEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finalization_id: str
    result_ref: str
    payload_hash: str
    payload: dict[str, Any]
    persisted_at: datetime


def _canonical_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


class FileSystemFinalizationRepository(_IR2FinalizationRepository):
    """IR-7 exact-session commands over the existing finalization model."""

    def _session_state(self, session_id: str) -> SessionInputRuntimeState:
        path = self.layout.state(session_id)
        if not path.exists():
            raise InputRuntimeNotFoundError("finalization session state is missing")
        return read_model(path, SessionInputRuntimeState)

    def _result_path(self, record: CycleFinalizationRecord):
        return (
            self.layout.cycle_dir(record.cycle_id)
            / "finalization-results"
            / f"{storage_key(record.finalization_id)}.json"
        )

    @staticmethod
    def _result_ref(finalization_id: str, payload_hash: str) -> str:
        return f"finalization-result:{finalization_id}:{payload_hash}"

    @staticmethod
    def _abort_kind(
        state: SessionInputRuntimeState,
        record: CycleFinalizationRecord,
    ) -> tuple[FinalizationState | None, str | None]:
        if (
            state.generation != record.generation
            or state.active_cycle_id != record.cycle_id
        ):
            return FinalizationState.ABORTED_CONTROL, "cycle_authority_changed"
        if state.active_context_revision_id != record.context_revision_id:
            return FinalizationState.ABORTED_NEW_INPUT, "context_revision_changed"
        if (
            state.active_cycle_accepted_through_sequence
            != record.expected_accepted_sequence
            or state.active_cycle_applied_through_sequence
            != record.expected_applied_sequence
            or state.active_cycle_accepted_through_sequence
            != state.active_cycle_applied_through_sequence
        ):
            return FinalizationState.ABORTED_NEW_INPUT, "new_input_before_terminal"
        if (
            state.pending_control_sequence != record.expected_control_sequence
            or state.applied_control_sequence != record.expected_control_sequence
            or state.pending_control_sequence != state.applied_control_sequence
        ):
            return FinalizationState.ABORTED_CONTROL, "control_before_terminal"
        if state.cycle_status in {
            CycleStatus.PAUSE_REQUESTED,
            CycleStatus.PAUSED_BY_USER,
            CycleStatus.CANCELLED,
            CycleStatus.INTERRUPTED,
        }:
            return FinalizationState.ABORTED_CONTROL, "cycle_control_state_changed"
        return None, None

    def _write_abort_locked(
        self,
        record: CycleFinalizationRecord,
        *,
        state: FinalizationState,
        reason: str,
        now: datetime,
    ) -> CycleFinalizationRecord:
        updates: dict[str, Any] = {
            "state": state,
            "updated_at": now,
            "failure_code": None,
            "cancellation_reason_code": (
                reason if state == FinalizationState.ABORTED_CONTROL else None
            ),
        }
        aborted = validated_copy(record, **updates)
        atomic_write_model(
            self.layout.finalization(record.cycle_id, record.finalization_id),
            aborted,
        )
        current_state = self._session_state(record.session_id)
        if (
            current_state.generation == record.generation
            and current_state.active_cycle_id == record.cycle_id
            and current_state.cycle_status == CycleStatus.FINALIZING
            and current_state.finalization_id == record.finalization_id
        ):
            resumed = validated_copy(
                current_state,
                cycle_status=CycleStatus.RUNNING,
                finalization_id=None,
                revision=current_state.revision + 1,
                updated_at=max(current_state.updated_at, now),
            )
            atomic_write_model(self.layout.state(record.session_id), resumed)
        return aborted

    async def prepare_authority(
        self,
        record: CycleFinalizationRecord,
    ) -> CycleFinalizationRecord:
        existing = await self.get(record.finalization_id)
        if existing is None:
            current = await super().prepare(record)
        else:
            if _final_identity(existing) != _final_identity(record):
                raise InputRuntimeConflictError("finalization identity changed")
            current = existing
        if current.state != FinalizationState.PREPARED:
            return current

        async with self.locks.hold(self.root, record.session_id):
            current = await self.get(record.finalization_id)
            if current is None:
                raise InputRuntimeNotFoundError(record.finalization_id)
            if current.state != FinalizationState.PREPARED:
                return current
            session = self._session_state(record.session_id)
            abort_state, reason = self._abort_kind(session, current)
            if abort_state is not None:
                return self._write_abort_locked(
                    current,
                    state=abort_state,
                    reason=reason or "finalization_authority_changed",
                    now=record.updated_at,
                )
            if session.cycle_status == CycleStatus.FINALIZING:
                if session.finalization_id != current.finalization_id:
                    return self._write_abort_locked(
                        current,
                        state=FinalizationState.ABORTED_CONTROL,
                        reason="another_finalization_owns_cycle",
                        now=record.updated_at,
                    )
                return current
            if session.cycle_status != CycleStatus.RUNNING:
                return self._write_abort_locked(
                    current,
                    state=FinalizationState.ABORTED_CONTROL,
                    reason="cycle_not_running_at_prepare",
                    now=record.updated_at,
                )
            finalizing = validated_copy(
                session,
                cycle_status=CycleStatus.FINALIZING,
                finalization_id=current.finalization_id,
                revision=session.revision + 1,
                updated_at=max(session.updated_at, record.updated_at),
            )
            atomic_write_model(self.layout.state(record.session_id), finalizing)
            return current

    async def persist_result_payload(
        self,
        finalization_id: str,
        *,
        result_payload: dict[str, Any],
        persisted_at: datetime,
    ) -> CycleFinalizationRecord:
        record = await self.get(finalization_id)
        if record is None:
            raise InputRuntimeNotFoundError(finalization_id)
        if record.state in {
            FinalizationState.ABORTED_NEW_INPUT,
            FinalizationState.ABORTED_CONTROL,
            FinalizationState.FAILED_TERMINAL,
        }:
            return record
        payload_bytes = _canonical_payload(result_payload)
        payload_hash = "sha256:" + hashlib.sha256(payload_bytes).hexdigest()
        result_ref = self._result_ref(finalization_id, payload_hash)
        envelope = _FinalResultEnvelope(
            finalization_id=finalization_id,
            result_ref=result_ref,
            payload_hash=payload_hash,
            payload=result_payload,
            persisted_at=persisted_at,
        )
        result_path = self._result_path(record)
        if result_path.exists():
            existing = read_model(result_path, _FinalResultEnvelope)
            if (
                existing.finalization_id != finalization_id
                or existing.payload_hash != payload_hash
                or existing.payload != result_payload
            ):
                raise InputRuntimeConflictError("final result replay conflicts")
        else:
            atomic_write_model(result_path, envelope)

        async with self.locks.hold(self.root, record.session_id):
            current = await self.get(finalization_id)
            if current is None:
                raise InputRuntimeNotFoundError(finalization_id)
            if current.state in {
                FinalizationState.ABORTED_NEW_INPUT,
                FinalizationState.ABORTED_CONTROL,
                FinalizationState.FAILED_TERMINAL,
            }:
                return current
            if current.result_ref is not None and current.result_ref != result_ref:
                raise InputRuntimeConflictError("final result identity changed")
            if current.state in {
                FinalizationState.RESULT_PERSISTED,
                FinalizationState.OUTPUT_READY,
                FinalizationState.TERMINAL_COMMITTED,
            }:
                return current
            if current.state != FinalizationState.PREPARED:
                raise InputRuntimeConflictError("finalization cannot persist result")
            updated = validated_copy(
                current,
                state=FinalizationState.RESULT_PERSISTED,
                result_ref=result_ref,
                updated_at=max(current.updated_at, persisted_at),
            )
            atomic_write_model(
                self.layout.finalization(current.cycle_id, current.finalization_id),
                updated,
            )
            return updated

    async def mark_output_ready(
        self,
        finalization_id: str,
        *,
        output_batch_id: str,
        persisted_at: datetime,
    ) -> CycleFinalizationRecord:
        record = await self.get(finalization_id)
        if record is None:
            raise InputRuntimeNotFoundError(finalization_id)
        async with self.locks.hold(self.root, record.session_id):
            current = await self.get(finalization_id)
            if current is None:
                raise InputRuntimeNotFoundError(finalization_id)
            if current.state in {
                FinalizationState.ABORTED_NEW_INPUT,
                FinalizationState.ABORTED_CONTROL,
                FinalizationState.FAILED_TERMINAL,
            }:
                return current
            if current.state == FinalizationState.TERMINAL_COMMITTED:
                if current.output_batch_id != output_batch_id:
                    raise InputRuntimeConflictError("terminal output identity changed")
                return current
            if current.state == FinalizationState.OUTPUT_READY:
                if current.output_batch_id != output_batch_id:
                    raise InputRuntimeConflictError("output batch identity changed")
                return current
            if current.state != FinalizationState.RESULT_PERSISTED:
                raise InputRuntimeConflictError("result must persist before output")
            updated = validated_copy(
                current,
                state=FinalizationState.OUTPUT_READY,
                output_batch_id=output_batch_id,
                updated_at=max(current.updated_at, persisted_at),
            )
            atomic_write_model(
                self.layout.finalization(current.cycle_id, current.finalization_id),
                updated,
            )
            return updated

    def _sync_snapshot_terminal_locked(
        self,
        record: CycleFinalizationRecord,
        *,
        terminal_status: CycleStatus,
        committed_at: datetime,
    ) -> None:
        path = self.layout.snapshot(record.cycle_id)
        if not path.exists():
            return
        snapshot = read_model(path, ActiveCycleSnapshot)
        if (
            snapshot.session_id != record.session_id
            or snapshot.generation != record.generation
        ):
            raise InputRuntimeConflictError("terminal snapshot authority changed")
        if snapshot.status == terminal_status:
            return
        updated = validated_copy(
            snapshot,
            status=terminal_status,
            waiting_question=None,
            pause_reason=None,
            interruption_reason=None,
            cancellation_reason_code=(
                "finalization_cancelled"
                if terminal_status == CycleStatus.CANCELLED
                else None
            ),
            snapshot_revision=snapshot.snapshot_revision + 1,
            updated_at=max(snapshot.updated_at, committed_at),
        )
        atomic_write_model(path, updated)

    async def commit_terminal_authority(
        self,
        finalization_id: str,
        *,
        terminal_status: CycleStatus,
        committed_at: datetime,
    ) -> CycleFinalizationRecord:
        record = await self.get(finalization_id)
        if record is None:
            raise InputRuntimeNotFoundError(finalization_id)
        async with self.locks.hold(self.root, record.session_id):
            current = await self.get(finalization_id)
            if current is None:
                raise InputRuntimeNotFoundError(finalization_id)
            if current.state == FinalizationState.TERMINAL_COMMITTED:
                return current
            if current.state in {
                FinalizationState.ABORTED_NEW_INPUT,
                FinalizationState.ABORTED_CONTROL,
                FinalizationState.FAILED_TERMINAL,
            }:
                return current
            if current.state != FinalizationState.OUTPUT_READY:
                raise InputRuntimeConflictError("output must be ready before terminal commit")
            session = self._session_state(current.session_id)
            abort_state, reason = self._abort_kind(session, current)
            partial_terminal = (
                session.generation == current.generation
                and session.active_cycle_id == current.cycle_id
                and session.finalization_id == current.finalization_id
                and session.cycle_status == terminal_status
                and session.active_cycle_accepted_through_sequence
                == current.expected_accepted_sequence
                and session.active_cycle_applied_through_sequence
                == current.expected_applied_sequence
                and session.pending_control_sequence
                == current.expected_control_sequence
                and session.applied_control_sequence
                == current.expected_control_sequence
            )
            if abort_state is not None and not partial_terminal:
                return self._write_abort_locked(
                    current,
                    state=abort_state,
                    reason=reason or "terminal_authority_changed",
                    now=committed_at,
                )
            if not partial_terminal and (
                session.cycle_status != CycleStatus.FINALIZING
                or session.finalization_id != current.finalization_id
            ):
                return self._write_abort_locked(
                    current,
                    state=FinalizationState.ABORTED_CONTROL,
                    reason="finalization_ownership_lost",
                    now=committed_at,
                )

            self._sync_snapshot_terminal_locked(
                current,
                terminal_status=terminal_status,
                committed_at=committed_at,
            )
            if not partial_terminal:
                terminal_session = validated_copy(
                    session,
                    cycle_status=terminal_status,
                    finalization_id=current.finalization_id,
                    revision=session.revision + 1,
                    updated_at=max(session.updated_at, committed_at),
                )
                atomic_write_model(
                    self.layout.state(current.session_id),
                    terminal_session,
                )
            committed = validated_copy(
                current,
                state=FinalizationState.TERMINAL_COMMITTED,
                updated_at=max(current.updated_at, committed_at),
            )
            # This is the delivery linearization marker.  It is deliberately the
            # last write so a partial multi-file commit never exposes output.
            atomic_write_model(
                self.layout.finalization(current.cycle_id, current.finalization_id),
                committed,
            )
            return committed

    async def commit_waiting_authority(
        self,
        *,
        session_id: str,
        cycle_id: str,
        generation: int,
        context_revision_id: str,
        expected_input_sequence: int,
        expected_control_sequence: int,
        waiting_question: str,
        committed_at: datetime,
    ) -> SessionInputRuntimeState:
        question = waiting_question.strip()
        if not question:
            raise ValueError("waiting question must not be empty")
        async with self.locks.hold(self.root, session_id):
            session = self._session_state(session_id)
            if session.generation != generation or session.active_cycle_id != cycle_id:
                raise InputRuntimeConflictError("waiting_aborted_control")
            if session.active_context_revision_id != context_revision_id:
                raise InputRuntimeConflictError("waiting_aborted_new_input")
            if (
                session.active_cycle_accepted_through_sequence
                != expected_input_sequence
                or session.active_cycle_applied_through_sequence
                != expected_input_sequence
            ):
                raise InputRuntimeConflictError("waiting_aborted_new_input")
            if (
                session.pending_control_sequence != expected_control_sequence
                or session.applied_control_sequence != expected_control_sequence
            ):
                raise InputRuntimeConflictError("waiting_aborted_control")
            if session.cycle_status == CycleStatus.WAITING_USER:
                return session
            if session.cycle_status != CycleStatus.RUNNING:
                raise InputRuntimeConflictError("waiting_aborted_control")

            snapshot_path = self.layout.snapshot(cycle_id)
            if snapshot_path.exists():
                snapshot = read_model(snapshot_path, ActiveCycleSnapshot)
                if (
                    snapshot.session_id != session_id
                    or snapshot.generation != generation
                    or snapshot.active_context_revision_id != context_revision_id
                ):
                    raise InputRuntimeConflictError("waiting_snapshot_authority_changed")
                waiting_snapshot = validated_copy(
                    snapshot,
                    status=CycleStatus.WAITING_USER,
                    waiting_question=question,
                    pause_reason=None,
                    interruption_reason=None,
                    snapshot_revision=snapshot.snapshot_revision + 1,
                    updated_at=max(snapshot.updated_at, committed_at),
                )
                atomic_write_model(snapshot_path, waiting_snapshot)
            waiting = validated_copy(
                session,
                cycle_status=CycleStatus.WAITING_USER,
                finalization_id=None,
                revision=session.revision + 1,
                updated_at=max(session.updated_at, committed_at),
            )
            atomic_write_model(self.layout.state(session_id), waiting)
            return waiting

    async def output_delivery_allowed(
        self,
        *,
        session_id: str,
        cycle_id: str,
        output_batch_id: str,
    ) -> bool:
        if not session_id or not cycle_id or not output_batch_id:
            return False
        records = list_models(
            self.layout.finalizations(cycle_id),
            CycleFinalizationRecord,
        )
        matching = [
            item
            for item in records
            if item.session_id == session_id
            and item.cycle_id == cycle_id
            and item.output_batch_id == output_batch_id
        ]
        if len(matching) != 1:
            return False
        return matching[0].state == FinalizationState.TERMINAL_COMMITTED
