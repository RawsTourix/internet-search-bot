"""Production Api lifecycle integration for IR-8 startup/shutdown recovery."""

from __future__ import annotations

import asyncio
from types import MethodType
from typing import Any
from uuid import uuid4

from ..core.models import AgentResult
from ..ingress.recovery import FileSystemCommittedInputBatchRecoveryReader
from ..input_runtime.recovery import (
    InputRuntimeReadinessGate,
    InputRuntimeRecoveryCoordinator,
    InputRuntimeRecoveryError,
    RecoveryDisposition,
)
from ..interaction.output_startup_recovery import reconcile_unclaimable_legacy_ready
from ..runtime.input_runtime_rehydration import rehydrate_active_agent_cycle
from .input_runtime_recovery_dependencies import (
    RecoveredRuntimeDependencies,
    validate_recovered_runtime_dependencies,
)


class _FinalOutputRecovery:
    """Local-only RESULT_PERSISTED -> same logical OutputBatch bridge."""

    def __init__(self, api: Any) -> None:
        self.api = api

    async def recover_final_output(
        self,
        *,
        record,
        result_payload: dict[str, Any],
    ) -> str:
        snapshot = await self.api.input_runtime_repositories.snapshots.get(
            record.cycle_id
        )
        if snapshot is None:
            raise InputRuntimeRecoveryError("finalization_snapshot_missing")
        batch, capability_snapshot = await self.api._resolve_batch_and_capability(
            snapshot.original_input_batch_id,
            session_id=record.session_id,
        )
        result = AgentResult.model_validate(result_payload)
        if result.session_id not in {None, record.session_id}:
            raise InputRuntimeRecoveryError("persisted_result_session_mismatch")
        if result.cycle_id not in {None, record.cycle_id}:
            raise InputRuntimeRecoveryError("persisted_result_cycle_mismatch")
        result.session_id = record.session_id
        result.cycle_id = record.cycle_id
        output = await self.api.output_assembler.assemble_final(
            result=result,
            input_batch=batch,
            capability_snapshot=capability_snapshot,
            locale=batch.locale or "ru",
        )
        if (
            record.output_batch_id is not None
            and output.output_batch_id != record.output_batch_id
        ):
            raise InputRuntimeRecoveryError("finalization_output_identity_mismatch")
        return output.output_batch_id


def _ensure_components(api: Any) -> None:
    if hasattr(api, "input_runtime_readiness_gate"):
        return
    gate = InputRuntimeReadinessGate()
    committed = FileSystemCommittedInputBatchRecoveryReader(
        api.ingress_services.batch_store
    )
    api.input_runtime_readiness_gate = gate
    api.input_runtime_recovery = InputRuntimeRecoveryCoordinator(
        repositories=api.input_runtime_repositories,
        admission_service=api.input_admission_service,
        committed_batches=committed,
        readiness_gate=gate,
        generation_coordinator=api.execution_coordinator,
        final_output_recovery=_FinalOutputRecovery(api),
    )
    api.input_runtime_recovery_plan = None
    api.input_runtime_recovery_report = None
    api.input_runtime_recovery_dependencies = RecoveredRuntimeDependencies(
        active_plan_states={}
    )
    api._ir8_blocked_cycles: dict[str, str] = {}
    api._ir8_runner_tasks: set[asyncio.Task[Any]] = set()


def _require_ready(api: Any) -> None:
    _ensure_components(api)
    api.input_runtime_readiness_gate.require_ready()


def _track_runner(api: Any, task: asyncio.Task[Any], logger: Any) -> None:
    api._ir8_runner_tasks.add(task)

    def completed(done: asyncio.Task[Any]) -> None:
        api._ir8_runner_tasks.discard(done)
        if done.cancelled():
            return
        try:
            done.result()
        except Exception:
            logger.exception(
                "Recovered input-runtime runner failed task=%s",
                done.get_name(),
            )

    task.add_done_callback(completed)


async def _resume_recovered_safe_cycle(api: Any, session_plan: Any) -> None:
    snapshot = session_plan.snapshot
    if snapshot is None:
        raise InputRuntimeRecoveryError("recovered_runner_snapshot_missing")
    admission = await api.input_runtime_repositories.admissions.get_by_input_batch_id(
        snapshot.original_input_batch_id
    )
    if admission is None:
        raise InputRuntimeRecoveryError("recovered_runner_admission_missing")
    if (
        admission.target_cycle_id != snapshot.cycle_id
        or admission.admitted_generation != snapshot.generation
    ):
        raise InputRuntimeRecoveryError("recovered_runner_admission_mismatch")
    marker = await api.input_admission_service.get_runtime_handoff(admission)
    if marker is not None:
        raise InputRuntimeRecoveryError("recovered_runner_handoff_not_safe")

    handoff_token: str | None = None
    try:
        async with api.execution_coordinator.admitted_run_lease(
            session_id=snapshot.session_id,
            input_batch_id=snapshot.original_input_batch_id,
            cycle_id=snapshot.cycle_id,
            expected_generation=snapshot.generation,
        ) as acquired:
            if not acquired:
                return
            batch, capability_snapshot = await api._resolve_batch_and_capability(
                snapshot.original_input_batch_id,
                session_id=snapshot.session_id,
            )
            handoff_token = uuid4().hex
            owns_handoff = await api.input_admission_service.begin_runtime_handoff(
                admission,
                handoff_token=handoff_token,
            )
            if not owns_handoff:
                return
            result = await api.mcp_client.resume_controlled_cycle(
                session_id=snapshot.session_id,
                cycle_id=snapshot.cycle_id,
                client_type=batch.client_type,
                progress_locale=batch.locale or "ru",
            )
            if result is None:
                raise InputRuntimeRecoveryError("recovered_cycle_not_installed")
            await api.input_admission_service.record_cycle_status(
                session_id=snapshot.session_id,
                cycle_id=snapshot.cycle_id,
                status=api._cycle_status_from_result(result),
            )
            await api._assemble_final_if_needed(
                result=result,
                batch=batch,
                capability_snapshot=capability_snapshot,
                progress_locale=batch.locale or "ru",
            )
            await api.input_admission_service.complete_runtime_handoff(
                admission,
                handoff_token=handoff_token,
            )
    except asyncio.CancelledError:
        await api._await_cancellation_cleanup(
            api._cleanup_initial_runtime_failure(
                admission,
                handoff_token=handoff_token,
                error_code="recovered_runtime_cancelled",
            )
        )
        raise
    except Exception:
        await api._cleanup_initial_runtime_failure(
            admission,
            handoff_token=handoff_token,
            error_code="recovered_runtime_handoff_ambiguous",
        )
        raise


async def _schedule_recovered_runner(
    api: Any,
    session_plan: Any,
    *,
    original_start_admitted_cycle,
) -> None:
    gate = api.input_runtime_readiness_gate
    await gate.wait_ready()
    if session_plan.disposition == RecoveryDisposition.START_ADMITTED:
        outcome = session_plan.admission_outcome
        if outcome is None:
            raise InputRuntimeRecoveryError("recovered_start_admission_missing")
        await original_start_admitted_cycle(api, outcome)
        return
    if session_plan.disposition == RecoveryDisposition.AUTO_RESUME_SAFE:
        await _resume_recovered_safe_cycle(api, session_plan)


async def _install_recovered_runtime(
    api: Any,
    plan: Any,
    *,
    original_start_admitted_cycle,
    logger: Any,
) -> None:
    api._ir8_blocked_cycles = plan.blocked_cycles()
    installer = getattr(api.mcp_client, "install_recovered_cycle", None)
    if not callable(installer):
        raise InputRuntimeRecoveryError("mcp_recovered_cycle_installer_missing")

    recovered_plans = api.input_runtime_recovery_dependencies.active_plan_states
    for session_plan in plan.sessions:
        if session_plan.should_rehydrate:
            cycle = rehydrate_active_agent_cycle(session_plan.snapshot)
            cycle.active_plan_state = recovered_plans.get(session_plan.cycle_id)
            if cycle.active_plan_id is not None and cycle.active_plan_state is None:
                raise InputRuntimeRecoveryError("recovered_active_plan_state_missing")
            installer(cycle)
        if not session_plan.should_auto_schedule:
            continue
        await api.execution_coordinator.install_recovered_reservation(
            session_id=session_plan.session_id,
            cycle_id=session_plan.cycle_id,
            generation=session_plan.generation,
        )
        task = asyncio.create_task(
            _schedule_recovered_runner(
                api,
                session_plan,
                original_start_admitted_cycle=original_start_admitted_cycle,
            ),
            name=(
                "input-runtime-recovered:"
                f"{session_plan.session_id}:{session_plan.cycle_id}"
            ),
        )
        _track_runner(api, task, logger)


async def _cancel_recovered_tasks(api: Any) -> None:
    tasks = [task for task in api._ir8_runner_tasks if not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def install_input_runtime_recovery_lifecycle(api_module: Any) -> None:
    """Install IR-8 on production Api instances without mutating test shells."""

    Api = api_module.Api
    if getattr(Api, "_ir8_lifecycle_installed", False):
        return

    logger = api_module.logger
    APIError = api_module.APIError
    original_init = Api.__init__
    original_start_admitted_cycle = Api.start_admitted_cycle
    original_submit_input = Api.submit_input
    original_admit = Api.admit_committed_batch
    original_start_cycle = Api.start_admitted_cycle
    original_resume_cycle = Api.resume_admitted_cycle
    original_call_batch = Api.call_agent_batch
    original_call_agent = Api.call_agent

    async def start(self):
        _ensure_components(self)
        gate = self.input_runtime_readiness_gate
        mcp_connect_started = False
        try:
            removed_workspaces = await api_module.cleanup_stale_artifact_workspaces(
                self.artifact_services.workspace_manager,
                ttl_seconds=self.artifact_config.workspace_ttl_seconds,
            )
            if removed_workspaces:
                logger.warning(
                    "Removed %s stale artifact workspaces",
                    len(removed_workspaces),
                )

            recovered_deliveries = await api_module.recover_stale_delivery_claims(
                self.artifact_services.delivery_store,
                claim_timeout_seconds=(
                    self.artifact_config.delivery_claim_timeout_seconds
                ),
            )
            if recovered_deliveries:
                logger.warning(
                    "Recovered %s stale delivery claims as unknown",
                    len(recovered_deliveries),
                )

            committed_drafts = (
                await self.ingress_services.ingress_service.commit_ready_drafts()
            )
            if committed_drafts:
                logger.warning(
                    "Committed %s recovered input drafts for IR-8 admission recovery",
                    len(committed_drafts),
                )

            plan = await self.input_runtime_recovery.recover()
            self.input_runtime_recovery_plan = plan
            self.input_runtime_recovery_report = plan.report
            self.input_runtime_recovery_dependencies = (
                await validate_recovered_runtime_dependencies(self, plan)
            )

            expired_presentations = await (
                self.ingress_services.presentation_store.expire_stale_reservations(
                    timeout_seconds=(
                        self.interaction_config.input_presentation.reservation_timeout_seconds
                    )
                )
            )
            if expired_presentations:
                logger.warning(
                    "Expired %s stale unbound input presentations",
                    len(expired_presentations),
                )
            recoverable_presentations = (
                await self.ingress_services.presentation_store.list_recoverable()
            )
            if recoverable_presentations:
                logger.warning(
                    "Found %s recoverable input presentations; transport reconciliation is required",
                    len(recoverable_presentations),
                )

            reconciled_outputs = await self.output_store.reconcile_stale_claims(
                timeout_seconds=(
                    self.interaction_config.output_runtime.delivery_claim_timeout_seconds
                )
            )
            if reconciled_outputs:
                logger.warning(
                    "Reconciled %s stale output delivery claims as unknown; no automatic resend performed",
                    len(reconciled_outputs),
                )
            await reconcile_unclaimable_legacy_ready(
                self.output_store,
                self.artifact_services.delivery_store,
            )
            recoverable_outputs = await self.output_store.list_recoverable()
            if recoverable_outputs:
                ready_outputs = sum(
                    item.state.value == "ready" for item in recoverable_outputs
                )
                delivering_outputs = sum(
                    item.state.value == "delivering"
                    for item in recoverable_outputs
                )
                logger.warning(
                    "Found %s recoverable output batches (ready=%s, delivering=%s); no automatic resend performed",
                    len(recoverable_outputs),
                    ready_outputs,
                    delivering_outputs,
                )

            logger.info("Подключение к MCP-серверам после IR-8 reconciliation")
            mcp_connect_started = True
            await self.mcp_client.connect_to_servers(self.server_configs)
            await _install_recovered_runtime(
                self,
                plan,
                original_start_admitted_cycle=original_start_admitted_cycle,
                logger=logger,
            )
            gate.mark_ready()
        except Exception as error:
            if gate.state.value not in {"failed", "stopping", "stopped"}:
                gate.mark_failed(
                    getattr(error, "reason_code", "api_startup_recovery_failed")
                )
            if mcp_connect_started:
                try:
                    await self.mcp_client.cleanup()
                except Exception:
                    logger.exception("MCP cleanup failed after startup recovery error")
            raise APIError(f"Ошибка запуска API runtime: {error!r}") from error

    async def stop(self):
        _ensure_components(self)
        gate = self.input_runtime_readiness_gate
        gate.begin_stopping()
        try:
            await _cancel_recovered_tasks(self)
            await self.execution_coordinator.shutdown()
        finally:
            try:
                await self.mcp_client.cleanup()
            except Exception as error:
                logger.error("Ошибка при отключении от сервера: %s", error)
            finally:
                gate.mark_stopped()

    async def guarded_submit(self, *args, **kwargs):
        _require_ready(self)
        return await original_submit_input(self, *args, **kwargs)

    async def guarded_admit(self, *args, **kwargs):
        _require_ready(self)
        return await original_admit(self, *args, **kwargs)

    async def guarded_start_cycle(self, *args, **kwargs):
        _require_ready(self)
        return await original_start_cycle(self, *args, **kwargs)

    async def guarded_resume_cycle(self, *args, **kwargs):
        _require_ready(self)
        return await original_resume_cycle(self, *args, **kwargs)

    async def guarded_call_batch(self, *args, **kwargs):
        _require_ready(self)
        return await original_call_batch(self, *args, **kwargs)

    async def guarded_call_agent(self, *args, **kwargs):
        _require_ready(self)
        return await original_call_agent(self, *args, **kwargs)

    def install_instance(instance: Any) -> None:
        if getattr(instance, "_ir8_lifecycle_instance_installed", False):
            return
        instance.start = MethodType(start, instance)
        instance.stop = MethodType(stop, instance)
        instance.submit_input = MethodType(guarded_submit, instance)
        instance.admit_committed_batch = MethodType(guarded_admit, instance)
        instance.start_admitted_cycle = MethodType(guarded_start_cycle, instance)
        instance.resume_admitted_cycle = MethodType(guarded_resume_cycle, instance)
        instance.call_agent_batch = MethodType(guarded_call_batch, instance)
        instance.call_agent = MethodType(guarded_call_agent, instance)
        instance._ir8_lifecycle_instance_installed = True

    def init_with_ir8(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        install_instance(self)

    Api.__init__ = init_with_ir8
    install_instance(api_module.API)
    Api._ir8_lifecycle_installed = True
