from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.input_runtime._filesystem_common import _Layout
from src.input_runtime.coordination import SessionLockRegistry
from src.input_runtime.diagnostics import InputRuntimeDiagnosticsService
from src.input_runtime.handoff import RuntimeHandoffRecord, RuntimeHandoffState
from src.input_runtime.ir9_filesystem import FileSystemRuntimeDiagnosticsReader
from src.input_runtime.models import (
    AdmissionKind,
    AgentEmission,
    ControlCommandType,
    ControlState,
    CycleFinalizationRecord,
    CycleInboxItem,
    CycleStatus,
    EmissionState,
    FinalizationState,
    InputAdmissionRecord,
    SessionControlCommand,
    SessionInputRuntimeState,
    new_admission_id,
    new_context_revision_id,
    new_control_id,
    new_emission_id,
    new_finalization_id,
    new_inbox_item_id,
)
from src.input_runtime.serialization import atomic_write_model, storage_key


NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


def session_state(
    *,
    session_id: str = "session-a",
    cycle_id: str = "cycle-a",
    generation: int = 1,
    status: CycleStatus = CycleStatus.RUNNING,
    accepted_session: int = 1,
    accepted: int = 0,
    applied: int = 0,
    pending_control: int = 0,
    applied_control: int = 0,
    finalization_id: str | None = None,
    revision: int = 1,
) -> SessionInputRuntimeState:
    return SessionInputRuntimeState(
        session_id=session_id,
        generation=generation,
        active_cycle_id=None if status == CycleStatus.IDLE else cycle_id,
        cycle_status=status,
        accepted_through_session_sequence=accepted_session,
        active_cycle_accepted_through_sequence=accepted,
        active_cycle_applied_through_sequence=applied,
        pending_control_sequence=pending_control,
        applied_control_sequence=applied_control,
        active_context_revision_id=None,
        finalization_id=finalization_id,
        revision=revision,
        created_at=NOW - timedelta(minutes=10),
        updated_at=NOW,
    )


def admission_record(
    *,
    session_id: str = "session-a",
    cycle_id: str = "cycle-a",
    input_batch_id: str = "batch-initial",
    sequence: int = 0,
    generation: int = 1,
    admission_id: str | None = None,
    kind: AdmissionKind | None = None,
) -> InputAdmissionRecord:
    return InputAdmissionRecord(
        admission_id=admission_id or new_admission_id(),
        session_id=session_id,
        input_batch_id=input_batch_id,
        session_sequence=sequence + 1,
        target_cycle_id=cycle_id,
        cycle_sequence=sequence,
        admitted_generation=generation,
        payload_size_bytes=12,
        admission_kind=kind or (
            AdmissionKind.START_CYCLE
            if sequence == 0
            else AdmissionKind.CONTINUE_RUNNING
        ),
        idempotency_key=f"idem-{session_id}-{sequence}",
        admitted_at=NOW - timedelta(minutes=5) + timedelta(seconds=sequence),
    )


def inbox_record(admission: InputAdmissionRecord) -> CycleInboxItem:
    return CycleInboxItem(
        inbox_item_id=new_inbox_item_id(),
        admission_id=admission.admission_id,
        session_id=admission.session_id,
        cycle_id=admission.target_cycle_id,
        input_batch_id=admission.input_batch_id,
        cycle_sequence=admission.cycle_sequence,
        generation=admission.admitted_generation,
        payload_size_bytes=12,
        enqueued_at=NOW - timedelta(minutes=2),
    )


def emission_record(
    *,
    session_id: str = "session-a",
    cycle_id: str = "cycle-a",
    generation: int = 1,
    text: str = "TOP SECRET USER TEXT",
    route_secret: str = "CALLBACK-SECRET",
) -> AgentEmission:
    return AgentEmission(
        emission_id=new_emission_id(),
        session_id=session_id,
        cycle_id=cycle_id,
        generation=generation,
        context_revision_id=new_context_revision_id(),
        kind="intermediate",
        text=text,
        visibility="user",
        importance="normal",
        response_route={
            "client_type": "telegram",
            "message_id": 7,
            "callback_auth": route_secret,
            "local_path": "/internal/secret/path",
        },
        state=EmissionState.READY,
        idempotency_key=f"emit-{session_id}",
        created_at=NOW - timedelta(minutes=1),
    )


def write_admission(layout: _Layout, record: InputAdmissionRecord) -> None:
    atomic_write_model(layout.admission(record.session_id, record.admission_id), record)


def write_inbox(layout: _Layout, record: CycleInboxItem) -> None:
    atomic_write_model(layout.inbox_item(record.cycle_id, record.inbox_item_id), record)


def write_control(layout: _Layout, record: SessionControlCommand) -> None:
    atomic_write_model(layout.control(record.session_id, record.control_id), record)


def write_emission(layout: _Layout, record: AgentEmission) -> None:
    atomic_write_model(layout.emission(record.cycle_id, record.emission_id), record)


def write_finalization(layout: _Layout, record: CycleFinalizationRecord) -> None:
    atomic_write_model(layout.finalization(record.cycle_id, record.finalization_id), record)


def write_handoff(root, record: RuntimeHandoffRecord) -> None:
    atomic_write_model(
        root
        / "input-runtime"
        / "runtime-handoffs"
        / f"{storage_key(record.admission_id)}.json",
        record,
    )


def file_snapshot(root) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.mark.asyncio
async def test_ir9_filesystem_status_is_exact_session_and_does_not_mutate_runtime(tmp_path):
    layout = _Layout(tmp_path)
    initial = admission_record()
    addition = admission_record(input_batch_id="batch-addition", sequence=1)
    atomic_write_model(layout.state("session-a"), session_state(accepted_session=2, accepted=1))
    write_admission(layout, initial)
    write_admission(layout, addition)
    write_inbox(layout, inbox_record(addition))
    before = file_snapshot(tmp_path)

    locks = SessionLockRegistry()
    diagnostics = InputRuntimeDiagnosticsService(
        FileSystemRuntimeDiagnosticsReader(root=tmp_path, locks=locks),
        clock=lambda: NOW,
    )
    status = await diagnostics.status("session-a")
    after = file_snapshot(tmp_path)

    assert status.session_exists is True
    assert status.input.accepted_sequence == 1
    assert status.input.queued == 1
    assert before == after


@pytest.mark.asyncio
async def test_ir9_filesystem_projection_never_leaks_emission_text_route_secret_or_path(tmp_path):
    layout = _Layout(tmp_path)
    atomic_write_model(layout.state("session-a"), session_state())
    secret_emission = emission_record()
    write_emission(layout, secret_emission)

    diagnostics = InputRuntimeDiagnosticsService(
        FileSystemRuntimeDiagnosticsReader(root=tmp_path, locks=SessionLockRegistry()),
        clock=lambda: NOW,
    )
    status = await diagnostics.status("session-a")
    timeline = await diagnostics.timeline("session-a", limit=20)
    payload = status.model_dump_json() + timeline.model_dump_json()

    assert status.emissions.ready == 1
    assert secret_emission.emission_id in payload
    assert "TOP SECRET USER TEXT" not in payload
    assert "CALLBACK-SECRET" not in payload
    assert "/internal/secret/path" not in payload
    assert "callback_auth" not in payload
    assert "response_route" not in payload


@pytest.mark.asyncio
async def test_ir9_filesystem_cross_session_isolation_with_equal_external_message_ids(tmp_path):
    layout = _Layout(tmp_path)
    atomic_write_model(layout.state("session-a"), session_state(session_id="session-a", cycle_id="cycle-a"))
    atomic_write_model(layout.state("session-b"), session_state(session_id="session-b", cycle_id="cycle-b"))
    emission_a = emission_record(session_id="session-a", cycle_id="cycle-a", route_secret="A-SECRET")
    emission_b = emission_record(session_id="session-b", cycle_id="cycle-b", route_secret="B-SECRET")
    write_emission(layout, emission_a)
    write_emission(layout, emission_b)

    diagnostics = InputRuntimeDiagnosticsService(
        FileSystemRuntimeDiagnosticsReader(root=tmp_path, locks=SessionLockRegistry()),
        clock=lambda: NOW,
    )
    status = await diagnostics.status("session-a")
    timeline = await diagnostics.timeline("session-a", limit=20)
    payload = status.model_dump_json() + timeline.model_dump_json()

    assert status.emissions.ready == 1
    assert emission_a.emission_id in payload
    assert emission_b.emission_id not in payload
    assert "B-SECRET" not in payload


@pytest.mark.asyncio
async def test_ir9_status_linearizes_after_concurrent_admission_boundary(tmp_path):
    layout = _Layout(tmp_path)
    locks = SessionLockRegistry()
    initial = admission_record()
    addition = admission_record(input_batch_id="batch-addition", sequence=1)
    atomic_write_model(layout.state("session-a"), session_state())
    write_admission(layout, initial)
    writer_entered = asyncio.Event()
    release_writer = asyncio.Event()

    async def writer():
        async with locks.hold(tmp_path, "session-a"):
            writer_entered.set()
            await release_writer.wait()
            write_admission(layout, addition)
            write_inbox(layout, inbox_record(addition))
            atomic_write_model(
                layout.state("session-a"),
                session_state(
                    accepted_session=2,
                    accepted=1,
                    revision=2,
                ),
            )

    writer_task = asyncio.create_task(writer())
    await writer_entered.wait()
    diagnostics = InputRuntimeDiagnosticsService(
        FileSystemRuntimeDiagnosticsReader(root=tmp_path, locks=locks),
        clock=lambda: NOW,
    )
    status_task = asyncio.create_task(diagnostics.status("session-a"))
    release_writer.set()
    status = await status_task
    await writer_task

    assert status.input.accepted_sequence == 1
    assert status.input.queued == 1
    assert [item.input_batch_id for item in status.additions] == ["batch-addition"]


@pytest.mark.asyncio
async def test_ir9_status_linearizes_after_concurrent_control_acceptance(tmp_path):
    layout = _Layout(tmp_path)
    locks = SessionLockRegistry()
    atomic_write_model(layout.state("session-a"), session_state())
    writer_entered = asyncio.Event()
    release_writer = asyncio.Event()
    command = SessionControlCommand(
        control_id=new_control_id(),
        session_id="session-a",
        target_cycle_id="cycle-a",
        generation=1,
        sequence_number=1,
        command=ControlCommandType.PAUSE,
        state=ControlState.ACKNOWLEDGED,
        idempotency_key="pause-1",
        source_client_type="telegram",
        source_message_ref={"message_id": 1},
        created_at=NOW - timedelta(seconds=2),
        acknowledged_at=NOW - timedelta(seconds=1),
    )

    async def writer():
        async with locks.hold(tmp_path, "session-a"):
            writer_entered.set()
            await release_writer.wait()
            write_control(layout, command)
            atomic_write_model(
                layout.state("session-a"),
                session_state(
                    status=CycleStatus.PAUSE_REQUESTED,
                    pending_control=1,
                    applied_control=0,
                    revision=2,
                ),
            )

    writer_task = asyncio.create_task(writer())
    await writer_entered.wait()
    diagnostics = InputRuntimeDiagnosticsService(
        FileSystemRuntimeDiagnosticsReader(root=tmp_path, locks=locks),
        clock=lambda: NOW,
    )
    status_task = asyncio.create_task(diagnostics.status("session-a"))
    release_writer.set()
    status = await status_task
    await writer_task

    assert status.session_status == CycleStatus.PAUSE_REQUESTED
    assert status.controls.pending_sequence == 1
    assert status.controls.pending_count == 1
    assert status.controls.effective_state == ControlState.ACKNOWLEDGED


@pytest.mark.asyncio
async def test_ir9_status_linearizes_after_terminal_commit_boundary(tmp_path):
    layout = _Layout(tmp_path)
    locks = SessionLockRegistry()
    initial = admission_record()
    write_admission(layout, initial)
    atomic_write_model(layout.state("session-a"), session_state())
    finalization_id = new_finalization_id()
    finalization = CycleFinalizationRecord(
        finalization_id=finalization_id,
        session_id="session-a",
        cycle_id="cycle-a",
        generation=1,
        context_revision_id=new_context_revision_id(),
        expected_accepted_sequence=0,
        expected_applied_sequence=0,
        expected_control_sequence=0,
        state=FinalizationState.TERMINAL_COMMITTED,
        result_ref="result-a",
        output_batch_id="output-a",
        created_at=NOW - timedelta(seconds=3),
        updated_at=NOW - timedelta(seconds=1),
    )
    handoff = RuntimeHandoffRecord(
        admission_id=initial.admission_id,
        session_id="session-a",
        input_batch_id=initial.input_batch_id,
        cycle_id="cycle-a",
        handoff_token="handoff-a",
        state=RuntimeHandoffState.COMPLETED,
        handed_off_at=NOW - timedelta(minutes=5),
        completed_at=NOW - timedelta(seconds=2),
    )
    writer_entered = asyncio.Event()
    release_writer = asyncio.Event()

    async def writer():
        async with locks.hold(tmp_path, "session-a"):
            writer_entered.set()
            await release_writer.wait()
            write_handoff(tmp_path, handoff)
            write_finalization(layout, finalization)
            atomic_write_model(
                layout.state("session-a"),
                session_state(
                    status=CycleStatus.DONE,
                    finalization_id=finalization_id,
                    revision=2,
                ),
            )

    writer_task = asyncio.create_task(writer())
    await writer_entered.wait()
    diagnostics = InputRuntimeDiagnosticsService(
        FileSystemRuntimeDiagnosticsReader(root=tmp_path, locks=locks),
        clock=lambda: NOW,
    )
    status_task = asyncio.create_task(diagnostics.status("session-a"))
    release_writer.set()
    status = await status_task
    await writer_task

    assert status.session_status == CycleStatus.DONE
    assert status.terminal is True
    assert status.finalization_state == FinalizationState.TERMINAL_COMMITTED
    assert status.handoff_state == RuntimeHandoffState.COMPLETED


@pytest.mark.asyncio
async def test_ir9_filesystem_reader_filters_old_generation_records_from_current_status(tmp_path):
    layout = _Layout(tmp_path)
    atomic_write_model(layout.state("session-a"), session_state(generation=2))
    current = emission_record(generation=2, text="current")
    stale = emission_record(generation=1, text="old-generation-secret")
    write_emission(layout, current)
    write_emission(layout, stale)

    diagnostics = InputRuntimeDiagnosticsService(
        FileSystemRuntimeDiagnosticsReader(root=tmp_path, locks=SessionLockRegistry()),
        clock=lambda: NOW,
    )
    status = await diagnostics.status("session-a")
    timeline = await diagnostics.timeline("session-a", limit=20)
    payload = status.model_dump_json() + timeline.model_dump_json()

    assert status.generation == 2
    assert status.emissions.ready == 1
    assert current.emission_id in payload
    assert stale.emission_id not in payload
    assert "old-generation-secret" not in payload
