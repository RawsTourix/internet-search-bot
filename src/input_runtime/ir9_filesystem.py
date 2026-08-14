"""Filesystem IR-9 coherent diagnostics reader.

Only bounded exact-session/current-cycle metadata is collected while holding the
existing short session coordination lock. No rendering, localization or network
work is performed under the lock.
"""

from __future__ import annotations

from pathlib import Path

from ._filesystem_common import _Layout
from .coordination import SessionLockRegistry
from .diagnostics import (
    DiagnosticAdmission,
    DiagnosticControl,
    DiagnosticEmission,
    DiagnosticFinalization,
    DiagnosticHandoff,
    DiagnosticInboxItem,
    RuntimeDiagnosticsError,
    RuntimeDiagnosticsRead,
)
from .handoff import RuntimeHandoffRecord
from .models import (
    AgentEmission,
    CycleFinalizationRecord,
    CycleInboxItem,
    InputAdmissionRecord,
    SessionControlCommand,
    SessionInputRuntimeState,
)
from .serialization import list_models, read_model, storage_key


class FileSystemRuntimeDiagnosticsReader:
    """Linearizable filesystem metadata read for one authoritative session."""

    def __init__(self, *, root: Path, locks: SessionLockRegistry) -> None:
        self.root = Path(root)
        self.locks = locks
        self.layout = _Layout(self.root)

    def _handoff_path(self, admission_id: str) -> Path:
        return (
            self.root
            / "input-runtime"
            / "runtime-handoffs"
            / f"{storage_key(admission_id)}.json"
        )

    @staticmethod
    def _admission(record: InputAdmissionRecord) -> DiagnosticAdmission:
        return DiagnosticAdmission(
            admission_id=record.admission_id,
            input_batch_id=record.input_batch_id,
            cycle_id=record.target_cycle_id,
            cycle_sequence=record.cycle_sequence,
            session_sequence=record.session_sequence,
            generation=record.admitted_generation,
            kind=record.admission_kind,
            state=record.state,
            admitted_at=record.admitted_at,
            applied_at=record.applied_at,
            cancelled_at=record.cancelled_at,
            failure_code=record.failure_code,
            cancellation_reason_code=record.cancellation_reason_code,
        )

    @staticmethod
    def _inbox(record: CycleInboxItem) -> DiagnosticInboxItem:
        return DiagnosticInboxItem(
            inbox_item_id=record.inbox_item_id,
            admission_id=record.admission_id,
            input_batch_id=record.input_batch_id,
            cycle_id=record.cycle_id,
            cycle_sequence=record.cycle_sequence,
            generation=record.generation,
            state=record.state,
            attempt_count=record.attempt_count,
            enqueued_at=record.enqueued_at,
            claimed_at=record.claimed_at,
            applied_at=record.applied_at,
            cancelled_at=record.cancelled_at,
            last_error_code=record.last_error_code,
        )

    @staticmethod
    def _control(record: SessionControlCommand) -> DiagnosticControl:
        return DiagnosticControl(
            control_id=record.control_id,
            target_cycle_id=record.target_cycle_id,
            generation=record.generation,
            sequence_number=record.sequence_number,
            command=record.command,
            state=record.state,
            created_at=record.created_at,
            acknowledged_at=record.acknowledged_at,
            applied_at=record.applied_at,
            rejection_code=record.rejection_code,
            cancellation_reason_code=record.cancellation_reason_code,
        )

    @staticmethod
    def _handoff(record: RuntimeHandoffRecord) -> DiagnosticHandoff:
        return DiagnosticHandoff(
            admission_id=record.admission_id,
            input_batch_id=record.input_batch_id,
            cycle_id=record.cycle_id,
            state=record.state,
            handed_off_at=record.handed_off_at,
            completed_at=record.completed_at,
            ambiguous_at=record.ambiguous_at,
            error_code=record.error_code,
        )

    @staticmethod
    def _emission(record: AgentEmission) -> DiagnosticEmission:
        # Deliberately do not copy text or response_route.
        return DiagnosticEmission(
            emission_id=record.emission_id,
            cycle_id=record.cycle_id,
            generation=record.generation,
            state=record.state,
            importance=record.importance,
            kind=record.kind,
            created_at=record.created_at,
            delivered_at=record.delivered_at,
            delivery_claimed_at=record.delivery_claimed_at,
            delivery_attempt_count=record.delivery_attempt_count,
            error_code=record.error_code,
            cancellation_reason_code=record.cancellation_reason_code,
        )

    @staticmethod
    def _finalization(record: CycleFinalizationRecord) -> DiagnosticFinalization:
        # result_ref may encode storage detail and is intentionally excluded.
        return DiagnosticFinalization(
            finalization_id=record.finalization_id,
            cycle_id=record.cycle_id,
            generation=record.generation,
            state=record.state,
            created_at=record.created_at,
            updated_at=record.updated_at,
            failure_code=record.failure_code,
            cancellation_reason_code=record.cancellation_reason_code,
            output_batch_id=record.output_batch_id,
        )

    @staticmethod
    def _assert_session(*, expected: str, actual: str, record_type: str) -> None:
        if actual != expected:
            raise RuntimeDiagnosticsError(
                f"diagnostics_{record_type}_session_mismatch"
            )

    async def read_session(self, session_id: str) -> RuntimeDiagnosticsRead:
        normalized = str(session_id).strip()
        if not normalized:
            raise RuntimeDiagnosticsError("invalid_session_id")

        async with self.locks.hold(self.root, normalized):
            state_path = self.layout.state(normalized)
            if not state_path.exists():
                return RuntimeDiagnosticsRead(session_id=normalized)

            state = read_model(state_path, SessionInputRuntimeState)
            self._assert_session(
                expected=normalized,
                actual=state.session_id,
                record_type="state",
            )
            generation = state.generation
            cycle_id = state.active_cycle_id

            admission_records = tuple(
                item
                for item in list_models(
                    self.layout.admissions(normalized),
                    InputAdmissionRecord,
                )
                if item.admitted_generation == generation
                and (cycle_id is None or item.target_cycle_id == cycle_id)
            )
            for item in admission_records:
                self._assert_session(
                    expected=normalized,
                    actual=item.session_id,
                    record_type="admission",
                )

            control_records = tuple(
                item
                for item in list_models(
                    self.layout.controls(normalized),
                    SessionControlCommand,
                )
                if item.generation == generation
            )
            for item in control_records:
                self._assert_session(
                    expected=normalized,
                    actual=item.session_id,
                    record_type="control",
                )

            inbox_records: tuple[CycleInboxItem, ...] = ()
            emission_records: tuple[AgentEmission, ...] = ()
            finalization_records: tuple[CycleFinalizationRecord, ...] = ()
            if cycle_id is not None:
                inbox_records = tuple(
                    item
                    for item in list_models(
                        self.layout.inbox(cycle_id),
                        CycleInboxItem,
                    )
                    if item.generation == generation
                )
                emission_records = tuple(
                    item
                    for item in list_models(
                        self.layout.emissions(cycle_id),
                        AgentEmission,
                    )
                    if item.generation == generation
                )
                finalization_records = tuple(
                    item
                    for item in list_models(
                        self.layout.finalizations(cycle_id),
                        CycleFinalizationRecord,
                    )
                    if item.generation == generation
                )
                for item in inbox_records:
                    self._assert_session(
                        expected=normalized,
                        actual=item.session_id,
                        record_type="inbox",
                    )
                    if item.cycle_id != cycle_id:
                        raise RuntimeDiagnosticsError(
                            "diagnostics_inbox_cycle_mismatch"
                        )
                for item in emission_records:
                    self._assert_session(
                        expected=normalized,
                        actual=item.session_id,
                        record_type="emission",
                    )
                    if item.cycle_id != cycle_id:
                        raise RuntimeDiagnosticsError(
                            "diagnostics_emission_cycle_mismatch"
                        )
                for item in finalization_records:
                    self._assert_session(
                        expected=normalized,
                        actual=item.session_id,
                        record_type="finalization",
                    )
                    if item.cycle_id != cycle_id:
                        raise RuntimeDiagnosticsError(
                            "diagnostics_finalization_cycle_mismatch"
                        )

            handoff_records: list[RuntimeHandoffRecord] = []
            for admission in admission_records:
                path = self._handoff_path(admission.admission_id)
                if not path.exists():
                    continue
                handoff = read_model(path, RuntimeHandoffRecord)
                self._assert_session(
                    expected=normalized,
                    actual=handoff.session_id,
                    record_type="handoff",
                )
                if (
                    handoff.admission_id != admission.admission_id
                    or handoff.input_batch_id != admission.input_batch_id
                    or handoff.cycle_id != admission.target_cycle_id
                ):
                    raise RuntimeDiagnosticsError(
                        "diagnostics_handoff_relation_mismatch"
                    )
                handoff_records.append(handoff)

            return RuntimeDiagnosticsRead(
                session_id=normalized,
                session=state,
                admissions=tuple(self._admission(item) for item in admission_records),
                inbox=tuple(self._inbox(item) for item in inbox_records),
                controls=tuple(self._control(item) for item in control_records),
                handoffs=tuple(self._handoff(item) for item in handoff_records),
                emissions=tuple(self._emission(item) for item in emission_records),
                finalizations=tuple(
                    self._finalization(item) for item in finalization_records
                ),
            )
