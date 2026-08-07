"""API-layer orchestration around the transport-neutral IR-5 control service."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.models import AgentResult
from ..input_runtime import ControlOutcome, ControlState, CycleStatus


@dataclass(frozen=True, slots=True)
class RuntimeControlRunResult:
    outcome: ControlOutcome
    agent_result: AgentResult | None = None


async def request_runtime_pause(
    api,
    *,
    session_id: str,
    idempotency_key: str,
    source_client_type: str,
    source_message_ref: dict[str, Any] | None = None,
    reason: str | None = None,
) -> RuntimeControlRunResult:
    outcome = await api.input_admission_service.control_service.request_pause(
        session_id=session_id,
        idempotency_key=idempotency_key,
        source_client_type=source_client_type,
        source_message_ref=source_message_ref,
        reason=reason,
    )
    return RuntimeControlRunResult(outcome=outcome)


async def request_runtime_continue(
    api,
    *,
    session_id: str,
    idempotency_key: str,
    source_client_type: str,
    source_message_ref: dict[str, Any] | None = None,
    reason: str | None = None,
    progress_callback=None,
    progress_locale: str = "ru",
) -> RuntimeControlRunResult:
    """Resume the same in-process durable cycle, never create a second cycle."""
    outcome = await api.input_admission_service.control_service.request_continue(
        session_id=session_id,
        idempotency_key=idempotency_key,
        source_client_type=source_client_type,
        source_message_ref=source_message_ref,
        reason=reason,
    )
    command = outcome.command
    if command.state == ControlState.REJECTED or command.target_cycle_id is None:
        return RuntimeControlRunResult(outcome=outcome)

    state = await api.input_runtime_repositories.sessions.get(session_id)
    if (
        state is None
        or state.generation != command.generation
        or state.active_cycle_id != command.target_cycle_id
    ):
        return RuntimeControlRunResult(outcome=outcome)

    # In a rapid pause/continue race the original runner still owns its lease.
    # The reducer neutralizes the pending pause and that runner continues; a
    # failed reacquisition here is therefore correct and prevents runner #2.
    # Do not synchronize the coordinator here: continue does not advance the
    # durable generation and the process-local cache must not become authority.
    async with api.execution_coordinator.admitted_run_lease(
        session_id=session_id,
        input_batch_id=f"control:{command.control_id}",
        cycle_id=command.target_cycle_id,
        expected_generation=state.generation,
    ) as acquired:
        if not acquired:
            return RuntimeControlRunResult(outcome=outcome)

        snapshot = await api.input_runtime_repositories.snapshots.get(
            command.target_cycle_id
        )
        if snapshot is None or snapshot.generation != state.generation:
            return RuntimeControlRunResult(outcome=outcome)

        # IR-8 owns reconstruction after process restart.  IR-5 only reacquires
        # a runner when this process still has the resumable pending cycle.
        can_resume = getattr(api.mcp_client, "can_resume_controlled_cycle", None)
        if not callable(can_resume) or not can_resume(
            session_id=session_id,
            cycle_id=command.target_cycle_id,
        ):
            return RuntimeControlRunResult(outcome=outcome)

        batch, capability_snapshot = await api._resolve_batch_and_capability(
            snapshot.original_input_batch_id,
            session_id=session_id,
        )
        result = await api.mcp_client.resume_controlled_cycle(
            session_id=session_id,
            cycle_id=command.target_cycle_id,
            client_type=batch.client_type,
            progress_callback=progress_callback,
            progress_locale=progress_locale,
        )
        if result is None:
            return RuntimeControlRunResult(outcome=outcome)

        await api.input_admission_service.record_cycle_status(
            session_id=session_id,
            cycle_id=command.target_cycle_id,
            status=api._cycle_status_from_result(result),
        )
        durable = await api.input_runtime_repositories.sessions.get(session_id)
        if (
            durable is not None
            and durable.generation == command.generation
            and durable.active_cycle_id == command.target_cycle_id
            and durable.cycle_status == CycleStatus.DONE
        ):
            await api._assemble_final_if_needed(
                result=result,
                batch=batch,
                capability_snapshot=capability_snapshot,
                progress_locale=progress_locale,
            )
        return RuntimeControlRunResult(outcome=outcome, agent_result=result)
