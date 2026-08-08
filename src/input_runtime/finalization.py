"""IR-7 durable finalization barrier application service.

The service owns stable logical finalization identity and delegates every
linearization-sensitive state change to command-oriented repository operations.
It contains no filesystem, HTTP, transport, Telegram, MCP or LLM knowledge.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .errors import InputRuntimeConflictError
from .factory import InputRuntimeRepositories
from .handoff import RuntimeHandoffState
from .handoff_context import get_runtime_handoff_context
from .models import (
    CycleFinalizationRecord,
    CycleStatus,
    FinalizationState,
    SessionInputRuntimeState,
)


Clock = Callable[[], datetime]


@dataclass(frozen=True, slots=True)
class FinalizationCandidate:
    finalization_id: str
    session_id: str
    cycle_id: str
    generation: int
    context_revision_id: str
    expected_accepted_sequence: int
    expected_applied_sequence: int
    expected_control_sequence: int
    runtime_handoff_admission_id: str | None = None
    runtime_handoff_token: str | None = None


@dataclass(frozen=True, slots=True)
class FinalizationPreparation:
    record: CycleFinalizationRecord | None
    abort_reason: str | None = None

    @property
    def prepared(self) -> bool:
        return self.record is not None and self.record.state in {
            FinalizationState.PREPARED,
            FinalizationState.RESULT_PERSISTED,
            FinalizationState.OUTPUT_READY,
            FinalizationState.TERMINAL_COMMITTED,
        }


class FinalizationBarrierService:
    """Coordinate one stable finalization lifecycle for an exact cycle view."""

    def __init__(
        self,
        *,
        repositories: InputRuntimeRepositories,
        clock: Clock | None = None,
    ) -> None:
        self.repositories = repositories
        self.repository = repositories.finalizations
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("runtime clock must return timezone-aware datetime")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _stable_id(
        *,
        session_id: str,
        cycle_id: str,
        generation: int,
        context_revision_id: str,
        accepted_sequence: int,
        applied_sequence: int,
        control_sequence: int,
    ) -> str:
        raw = "\0".join(
            (
                session_id,
                cycle_id,
                str(generation),
                context_revision_id,
                str(accepted_sequence),
                str(applied_sequence),
                str(control_sequence),
            )
        ).encode("utf-8")
        return "fin_" + hashlib.sha256(raw).hexdigest()[:32]

    async def capture_candidate(
        self,
        *,
        session_id: str,
        cycle_id: str,
    ) -> FinalizationCandidate:
        state = await self.repositories.sessions.get(session_id)
        if state is None:
            raise InputRuntimeConflictError("finalization session state is missing")
        if state.active_cycle_id != cycle_id:
            raise InputRuntimeConflictError("finalization active cycle authority changed")
        if state.cycle_status not in {CycleStatus.RUNNING, CycleStatus.FINALIZING}:
            raise InputRuntimeConflictError("cycle is not eligible for final processing")
        if state.active_context_revision_id is None:
            raise InputRuntimeConflictError("finalization context revision is missing")
        if (
            state.active_cycle_accepted_through_sequence
            != state.active_cycle_applied_through_sequence
        ):
            raise InputRuntimeConflictError("finalization blocked by unapplied input")
        if state.pending_control_sequence != state.applied_control_sequence:
            raise InputRuntimeConflictError("finalization blocked by pending control")

        handoff_admission_id: str | None = None
        handoff_token: str | None = None
        handoff_context = get_runtime_handoff_context()
        if handoff_context is not None:
            if (
                handoff_context.session_id != session_id
                or handoff_context.cycle_id != cycle_id
                or handoff_context.generation != state.generation
            ):
                raise InputRuntimeConflictError(
                    "finalization runtime handoff execution context changed"
                )
            marker = await self.repositories.handoffs.get(
                handoff_context.admission_id
            )
            if marker is None:
                raise InputRuntimeConflictError(
                    "finalization runtime handoff marker is missing"
                )
            if (
                marker.session_id != session_id
                or marker.cycle_id != cycle_id
                or marker.handoff_token != handoff_context.handoff_token
            ):
                raise InputRuntimeConflictError(
                    "finalization runtime handoff authority changed"
                )
            if marker.state != RuntimeHandoffState.HANDED_OFF:
                raise InputRuntimeConflictError(
                    "finalization requires an active runtime handoff"
                )
            handoff_admission_id = marker.admission_id
            handoff_token = marker.handoff_token

        return FinalizationCandidate(
            finalization_id=self._stable_id(
                session_id=session_id,
                cycle_id=cycle_id,
                generation=state.generation,
                context_revision_id=state.active_context_revision_id,
                accepted_sequence=state.active_cycle_accepted_through_sequence,
                applied_sequence=state.active_cycle_applied_through_sequence,
                control_sequence=state.pending_control_sequence,
            ),
            session_id=session_id,
            cycle_id=cycle_id,
            generation=state.generation,
            context_revision_id=state.active_context_revision_id,
            expected_accepted_sequence=(
                state.active_cycle_accepted_through_sequence
            ),
            expected_applied_sequence=(
                state.active_cycle_applied_through_sequence
            ),
            expected_control_sequence=state.pending_control_sequence,
            runtime_handoff_admission_id=handoff_admission_id,
            runtime_handoff_token=handoff_token,
        )

    async def prepare(
        self,
        candidate: FinalizationCandidate,
    ) -> FinalizationPreparation:
        now = self._now()
        record = CycleFinalizationRecord(
            finalization_id=candidate.finalization_id,
            session_id=candidate.session_id,
            cycle_id=candidate.cycle_id,
            generation=candidate.generation,
            context_revision_id=candidate.context_revision_id,
            expected_accepted_sequence=candidate.expected_accepted_sequence,
            expected_applied_sequence=candidate.expected_applied_sequence,
            expected_control_sequence=candidate.expected_control_sequence,
            state=FinalizationState.PREPARED,
            created_at=now,
            updated_at=now,
        )
        if candidate.runtime_handoff_admission_id is not None:
            if candidate.runtime_handoff_token is None:
                raise InputRuntimeConflictError(
                    "finalization runtime handoff token is missing"
                )
            binder = getattr(
                self.repository,
                "bind_runtime_handoff_authority",
                None,
            )
            if not callable(binder):
                raise InputRuntimeConflictError(
                    "finalization repository cannot bind runtime handoff authority"
                )
            await binder(
                finalization_id=record.finalization_id,
                session_id=record.session_id,
                cycle_id=record.cycle_id,
                admission_id=candidate.runtime_handoff_admission_id,
                handoff_token=candidate.runtime_handoff_token,
                bound_at=now,
            )
        current = await self.repository.prepare_authority(record)
        if current.state in {
            FinalizationState.ABORTED_NEW_INPUT,
            FinalizationState.ABORTED_CONTROL,
        }:
            return FinalizationPreparation(
                record=current,
                abort_reason=(
                    "new_input"
                    if current.state == FinalizationState.ABORTED_NEW_INPUT
                    else current.cancellation_reason_code or "control"
                ),
            )
        return FinalizationPreparation(record=current)

    async def persist_result(
        self,
        finalization_id: str,
        result_payload: dict[str, Any],
    ) -> CycleFinalizationRecord:
        return await self.repository.persist_result_payload(
            finalization_id,
            result_payload=result_payload,
            persisted_at=self._now(),
        )

    async def mark_output_ready(
        self,
        finalization_id: str,
        *,
        output_batch_id: str,
    ) -> CycleFinalizationRecord:
        return await self.repository.mark_output_ready(
            finalization_id,
            output_batch_id=output_batch_id,
            persisted_at=self._now(),
        )

    async def terminal_commit(
        self,
        finalization_id: str,
        *,
        status: CycleStatus = CycleStatus.DONE,
    ) -> CycleFinalizationRecord:
        if status not in {CycleStatus.DONE, CycleStatus.ERROR, CycleStatus.CANCELLED}:
            raise ValueError("terminal commit requires terminal cycle status")
        return await self.repository.commit_terminal_authority(
            finalization_id,
            terminal_status=status,
            committed_at=self._now(),
        )

    async def commit_waiting(
        self,
        *,
        session_id: str,
        cycle_id: str,
        generation: int,
        context_revision_id: str,
        expected_input_sequence: int,
        expected_control_sequence: int,
        waiting_question: str,
    ) -> SessionInputRuntimeState:
        return await self.repository.commit_waiting_authority(
            session_id=session_id,
            cycle_id=cycle_id,
            generation=generation,
            context_revision_id=context_revision_id,
            expected_input_sequence=expected_input_sequence,
            expected_control_sequence=expected_control_sequence,
            waiting_question=waiting_question,
            committed_at=self._now(),
        )

    async def output_delivery_allowed(self, batch: Any) -> bool:
        kind = str(getattr(getattr(batch, "kind", None), "value", getattr(batch, "kind", "")))
        if kind != "final":
            return True
        return bool(
            await self.repository.output_delivery_allowed(
                session_id=str(getattr(batch, "session_id", "")),
                cycle_id=str(getattr(batch, "cycle_id", "")),
                output_batch_id=str(getattr(batch, "output_batch_id", "")),
            )
        )
