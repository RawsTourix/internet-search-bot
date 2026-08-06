import logging
import os
from collections.abc import AsyncIterator, Mapping
from logging.handlers import RotatingFileHandler
from pathlib import Path
from uuid import uuid4

from .config import (
    AGENT_CONFIG_PATH,
    HTTP_PROXY,
    HTTPS_PROXY,
    safe_artifact_config_summary,
    safe_llm_config_summary,
    safe_memory_config_summary,
    safe_mcp_server_config_summary,
    safe_planning_config_summary,
    safe_runtime_config_summary,
)
from ..artifacts import (
    apply_local_workspace_server_policy,
    cleanup_stale_artifact_workspaces,
    create_artifact_services,
    load_artifact_config,
    recover_stale_delivery_claims,
)
from ..core.errors import APIError
from ..core.models import AgentResult, AgentStatus, ClientType
from ..ingress import (
    ClientInputEnvelope,
    InputSubmissionResult,
    create_ingress_services,
    load_ingress_config,
    resolve_input_grouping,
)
from ..input_runtime import (
    AdmissionKind,
    CycleStatus,
    InputAdmissionAction,
    InputAdmissionOutcome,
    InputAdmissionService,
    InputRuntimeConfigType,
    create_filesystem_input_runtime_repositories,
    load_input_runtime_config,
    safe_input_runtime_config_summary,
)
from ..mcp.artifact_delivery_runtime import (
    FinalizingArtifactDeliveryPlanningMCPClient,
)
from ..mcp.mcp_client import load_config
from ..planning import create_planning_services, load_planning_config
from ..planning.runtime_context import PlanningAwareContentStore
from ..storage import StorageServices, create_storage_services
from ..runtime import SessionExecutionCoordinator
from ..interaction.config import (
    load_interaction_config,
    safe_interaction_config_summary,
)
from ..interaction.capabilities import (
    build_cli_capability_declaration,
    build_telegram_capability_declaration,
    build_web_capability_declaration,
)
from ..interaction.output_service import OutputBatchAssembler
from ..interaction.output_store import FileSystemOutputBatchStore
from ..interaction.rendering import CapabilityOutputRenderer


os.environ["http_proxy"] = HTTP_PROXY
os.environ["https_proxy"] = HTTPS_PROXY

log_dir = "logging"
os.makedirs(log_dir, exist_ok=True)
formatter = logging.Formatter(
    "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("API")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    file_handler = RotatingFileHandler(
        filename=os.path.join(log_dir, "api.log"),
        maxBytes=8 * 1024 * 1024,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)


class Api:
    """Composition root for the agent runtime and durable input/artifact layers."""

    def __init__(self, config_path):
        try:
            self.execution_coordinator = SessionExecutionCoordinator()
            logger.info(
                "Загрузка конфигурации MCP, LLM, storage, memory, runtime, "
                "planning, artifacts, ingress и input-runtime"
            )
            (
                self.server_configs,
                self.llm_config,
                self.storage_config,
                self.memory_config,
                self.runtime_config,
            ) = load_config(config_path)
            self.planning_config = load_planning_config(config_path)
            self.artifact_config = load_artifact_config(config_path)
            self.ingress_config = load_ingress_config(config_path)
            self.interaction_config = load_interaction_config(config_path)
            self.input_runtime_config = (
                load_input_runtime_config(config_path)
                if config_path
                else InputRuntimeConfigType()
            )
            apply_local_workspace_server_policy(
                self.server_configs,
                self.artifact_config,
            )

            llm_summary = safe_llm_config_summary(self.llm_config)
            logger.debug(
                "LLM config: model=%s api_url=%s openai_compatible=%s "
                "context_window_tokens=%s tokenizer_encoding=%s final_audit=%s",
                llm_summary["model"],
                llm_summary["api_url"],
                llm_summary["openai_compatible"],
                llm_summary["context_window_tokens"],
                llm_summary["tokenizer_encoding"],
                llm_summary["final_audit"],
            )
            logger.debug(
                "MCP servers configured: %s",
                safe_mcp_server_config_summary(self.server_configs),
            )

            storage_root = Path(self.storage_config.root_dir).expanduser()
            if not storage_root.is_absolute():
                storage_root = Path.cwd() / storage_root
            logger.info(
                "Storage: backend=%s root=%s atomic_writes=%s "
                "verify_content_hash=%s max_in_memory_content_bytes=%s",
                self.storage_config.backend,
                storage_root.resolve(strict=False),
                self.storage_config.atomic_writes,
                self.storage_config.verify_content_hash,
                self.storage_config.max_in_memory_content_bytes,
            )
            logger.info(
                "Memory result compaction: %s",
                safe_memory_config_summary(self.memory_config),
            )
            logger.info(
                "Runtime lifecycle: %s",
                safe_runtime_config_summary(self.runtime_config),
            )
            logger.info(
                "DAG planning: %s",
                safe_planning_config_summary(self.planning_config),
            )
            logger.info(
                "Artifacts: %s",
                safe_artifact_config_summary(self.artifact_config),
            )
            logger.info(
                "Ingress: enabled=%s max_attachments=%s max_batch_bytes=%s",
                self.ingress_config.enabled,
                self.ingress_config.max_attachments_per_batch,
                self.ingress_config.max_batch_total_bytes,
            )
            logger.info(
                "Interaction: %s",
                safe_interaction_config_summary(self.interaction_config),
            )
            logger.info(
                "Input runtime: %s",
                safe_input_runtime_config_summary(self.input_runtime_config),
            )

            base_storage = create_storage_services(self.storage_config)
            self.storage_services = StorageServices(
                config=base_storage.config,
                content_store=PlanningAwareContentStore(
                    base_storage.content_store
                ),
                artifact_store=base_storage.artifact_store,
            )
            self.artifact_services = create_artifact_services(
                storage_config=self.storage_config,
                artifact_config=self.artifact_config,
                content_store=self.storage_services.content_store,
            )
            self.ingress_services = create_ingress_services(
                storage_config=self.storage_config,
                ingress_config=self.ingress_config,
                content_store=self.storage_services.content_store,
                artifact_services=self.artifact_services,
                interaction_config=self.interaction_config,
            )
            self.input_runtime_repositories = (
                create_filesystem_input_runtime_repositories(
                    storage_config=self.storage_config,
                )
            )
            self.input_admission_service = InputAdmissionService(
                config=self.input_runtime_config,
                repositories=self.input_runtime_repositories,
                committed_batches=self.ingress_services.batch_store,
                wake_coordinator=self.execution_coordinator,
            )

            output_root = Path(self.storage_config.root_dir).expanduser()
            if not output_root.is_absolute():
                output_root = Path.cwd() / output_root
            self.output_store = FileSystemOutputBatchStore(
                output_root.resolve(strict=False),
                atomic_writes=self.storage_config.atomic_writes,
            )
            from ..interaction.output_completion import (
                OutputDeliveryCompletionService,
            )

            self.output_completion = OutputDeliveryCompletionService(
                output_store=self.output_store,
                artifact_delivery_store=(
                    self.artifact_services.delivery_store
                ),
            )
            self.output_renderer = CapabilityOutputRenderer(
                self.ingress_services.localization_service,
                max_delivery_groups=(
                    self.interaction_config.output_runtime.max_delivery_groups
                ),
                prefer_document_groups=(
                    self.interaction_config.telegram_output.prefer_document_groups
                ),
            )
            self.output_assembler = OutputBatchAssembler(
                config=self.interaction_config.output_runtime,
                delivery_store=self.artifact_services.delivery_store,
                output_store=self.output_store,
                renderer=self.output_renderer,
            )
            self.planning_services = create_planning_services(
                storage_config=self.storage_config,
                planning_config=self.planning_config,
            )

            self.mcp_client = FinalizingArtifactDeliveryPlanningMCPClient(
                self.llm_config,
                storage_services=self.storage_services,
                artifact_services=self.artifact_services,
                memory_config=self.memory_config,
                runtime_config=self.runtime_config,
                planning_services=self.planning_services,
                defer_cycle_done_for_output=(
                    self.interaction_config.output_runtime.enabled
                ),
            )
            controller = getattr(
                self.mcp_client,
                "artifact_tool_controller",
                None,
            )
            if controller is not None:
                controller.committed_batch_store = (
                    self.ingress_services.batch_store
                )
        except Exception as error:
            raise APIError(f"Ошибка инициализации Api: {error!r}") from error

    async def start(self):
        """Recover durable state conservatively, then connect MCP servers."""
        try:
            removed_workspaces = await cleanup_stale_artifact_workspaces(
                self.artifact_services.workspace_manager,
                ttl_seconds=self.artifact_config.workspace_ttl_seconds,
            )
            if removed_workspaces:
                logger.warning(
                    "Removed %s stale artifact workspaces",
                    len(removed_workspaces),
                )

            recovered_deliveries = await recover_stale_delivery_claims(
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
                    "Committed %s recovered input drafts without automatic agent run",
                    len(committed_drafts),
                )

            expired_presentations = await (
                self.ingress_services.presentation_store
                .expire_stale_reservations(
                    timeout_seconds=(
                        self.interaction_config.input_presentation
                        .reservation_timeout_seconds
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
                    "Found %s recoverable input presentations; "
                    "transport reconciliation is required",
                    len(recoverable_presentations),
                )

            reconciled_outputs = await self.output_store.reconcile_stale_claims(
                timeout_seconds=(
                    self.interaction_config.output_runtime
                    .delivery_claim_timeout_seconds
                )
            )
            if reconciled_outputs:
                logger.warning(
                    "Reconciled %s stale output delivery claims as unknown; "
                    "no automatic resend performed",
                    len(reconciled_outputs),
                )

            recoverable_outputs = await self.output_store.list_recoverable()
            ready_outputs = sum(
                item.state.value == "ready" for item in recoverable_outputs
            )
            delivering_outputs = sum(
                item.state.value == "delivering"
                for item in recoverable_outputs
            )
            if recoverable_outputs:
                logger.warning(
                    "Found %s recoverable output batches "
                    "(ready=%s, delivering=%s); no automatic resend performed",
                    len(recoverable_outputs),
                    ready_outputs,
                    delivering_outputs,
                )

            logger.info("Подключение к MCP-серверам")
            await self.mcp_client.connect_to_servers(self.server_configs)
        except Exception as error:
            raise APIError(
                f"Ошибка запуска API runtime: {error!r}"
            ) from error

    async def submit_input(
        self,
        envelope: ClientInputEnvelope,
        *,
        session_id: str,
        upload_streams: Mapping[str, AsyncIterator[bytes]] | None = None,
    ) -> InputSubmissionResult:
        """Persist a client envelope using the shared logical-input policy."""
        try:
            list_open = getattr(
                self.ingress_services.batch_store,
                "list_open_drafts",
                None,
            )
            open_drafts = (
                await list_open(session_id=session_id)
                if list_open is not None
                else []
            )
            grouping = resolve_input_grouping(
                envelope,
                open_drafts=open_drafts,
            )
            logger.info(
                "Input grouping resolved: client=%s session_id=%s mode=%s "
                "joined_input_batch_id=%s source_message_id=%s",
                envelope.client_type.value,
                session_id,
                grouping.mode.value,
                grouping.joined_input_batch_id,
                envelope.source_message_id,
            )
            return await self.ingress_services.ingress_service.submit_atomic(
                envelope,
                session_id=session_id,
                upload_streams=upload_streams,
                grouping_mode=grouping.mode,
                grouping_key=grouping.key,
            )
        except Exception as error:
            logger.error("Ошибка durable ingress: %r", error)
            raise APIError(f"Ошибка приёма входного batch: {error}") from error

    async def admit_committed_batch(
        self,
        input_batch_id: str,
        *,
        session_id: str,
    ) -> InputAdmissionOutcome:
        """Route one authoritative committed batch through the IR-3 boundary."""
        if not self.input_runtime_config.enabled:
            raise APIError(
                "input_runtime is disabled; use explicit compatibility path"
            )
        try:
            return await self.input_admission_service.admit_committed_batch(
                input_batch_id,
                session_id=session_id,
            )
        except Exception as error:
            logger.error("Input admission failed: %r", error)
            raise APIError(f"Ошибка admission committed batch: {error}") from error

    async def _resolve_batch_and_capability(
        self,
        input_batch_id: str,
        *,
        session_id: str,
    ):
        batch = await self.ingress_services.batch_store.get_committed(
            input_batch_id
        )
        if batch.session_id != session_id:
            raise APIError("Input batch belongs to another session")
        capability_snapshot = batch.capability_snapshot
        if capability_snapshot is None:
            if batch.client_type == ClientType.TELEGRAM:
                declaration = build_telegram_capability_declaration(
                    document_grouping=(
                        self.interaction_config.telegram_output
                        .prefer_document_groups
                    ),
                    message_editing=(
                        self.interaction_config.telegram_output
                        .status_message_editing
                    ),
                )
            elif batch.client_type == ClientType.WEB:
                declaration = build_web_capability_declaration()
            else:
                declaration = build_cli_capability_declaration()
            capability_snapshot, _ = (
                await self.ingress_services.capability_store.resolve(
                    declaration,
                    client_type=batch.client_type.value,
                    client_instance_id=(
                        f"legacy-committed-batch:{batch.client_type.value}"
                    ),
                )
            )
        return batch, capability_snapshot

    async def _assemble_final_if_needed(
        self,
        *,
        result: AgentResult,
        batch,
        capability_snapshot,
        progress_locale: str,
    ) -> None:
        if (
            result.status == AgentStatus.DONE
            and self.interaction_config.output_runtime.enabled
        ):
            await self.output_assembler.assemble_final(
                result=result,
                input_batch=batch,
                capability_snapshot=capability_snapshot,
                locale=batch.locale or progress_locale,
            )

    @staticmethod
    def _cycle_status_from_result(result: AgentResult) -> CycleStatus:
        if result.status == AgentStatus.WAITING_USER:
            return CycleStatus.WAITING_USER
        if result.status == AgentStatus.DONE:
            return CycleStatus.DONE
        if result.status == AgentStatus.ERROR:
            return (
                CycleStatus.INTERRUPTED
                if result.can_resume
                else CycleStatus.ERROR
            )
        return CycleStatus.RUNNING

    async def _validate_admitted_runner_authority(
        self,
        outcome: InputAdmissionOutcome,
    ) -> None:
        admission = outcome.admission
        if admission is None or outcome.target_cycle_id is None:
            raise APIError("Admission outcome has no runner authority")
        current = await self.input_runtime_repositories.admissions.get_by_input_batch_id(
            outcome.input_batch_id
        )
        state = await self.input_runtime_repositories.sessions.get(
            outcome.session_id
        )
        if current is None or current.admission_id != admission.admission_id:
            raise APIError("Admission identity changed before runner start")
        if state is None:
            raise APIError("Session runtime state is missing")
        if (
            state.generation != admission.admitted_generation
            or state.active_cycle_id != admission.target_cycle_id
        ):
            raise APIError("Admitted cycle authority is stale")

    async def start_admitted_cycle(
        self,
        outcome: InputAdmissionOutcome,
        *,
        progress_callback=None,
        progress_locale: str = "ru",
    ) -> AgentResult | None:
        """Start exactly the cycle identity allocated by admission."""
        admission = outcome.admission
        if (
            admission is None
            or admission.admission_kind != AdmissionKind.START_CYCLE
            or not outcome.should_start_runner
        ):
            return None
        cycle_id = admission.target_cycle_id
        try:
            async with self.execution_coordinator.admitted_run_lease(
                session_id=admission.session_id,
                input_batch_id=admission.input_batch_id,
                cycle_id=cycle_id,
            ) as acquired:
                if not acquired:
                    return None
                await self._validate_admitted_runner_authority(outcome)
                batch, capability_snapshot = (
                    await self._resolve_batch_and_capability(
                        admission.input_batch_id,
                        session_id=admission.session_id,
                    )
                )
                result = await self.mcp_client.process_query(
                    "",
                    session_id=admission.session_id,
                    client_type=batch.client_type,
                    progress_callback=progress_callback,
                    progress_locale=progress_locale,
                    input_batch=batch,
                    cycle_id_override=cycle_id,
                )
                await self.input_admission_service.mark_initial_batch_applied(
                    admission
                )
                await self.input_admission_service.record_cycle_status(
                    session_id=admission.session_id,
                    cycle_id=cycle_id,
                    status=self._cycle_status_from_result(result),
                )
                await self._assemble_final_if_needed(
                    result=result,
                    batch=batch,
                    capability_snapshot=capability_snapshot,
                    progress_locale=progress_locale,
                )
                return result
        except APIError:
            raise
        except Exception as error:
            logger.error("Ошибка запуска admitted cycle: %r", error)
            try:
                await self.input_admission_service.record_cycle_status(
                    session_id=admission.session_id,
                    cycle_id=cycle_id,
                    status=CycleStatus.INTERRUPTED,
                )
            except Exception:
                logger.exception("Failed to record admitted runner interruption")
            raise APIError(f"Ошибка запуска admitted cycle: {error}") from error

    async def resume_admitted_cycle(
        self,
        outcome: InputAdmissionOutcome,
        *,
        progress_callback=None,
        progress_locale: str = "ru",
    ) -> AgentResult | None:
        """Compatibility adapter for WAITING_USER until IR-4."""
        admission = outcome.admission
        if (
            admission is None
            or admission.admission_kind != AdmissionKind.RESUME_WAITING
            or not outcome.should_wake_runner
        ):
            return None
        cycle_id = admission.target_cycle_id
        claim = None
        try:
            async with self.execution_coordinator.admitted_run_lease(
                session_id=admission.session_id,
                input_batch_id=admission.input_batch_id,
                cycle_id=cycle_id,
            ) as acquired:
                if not acquired:
                    return None
                await self._validate_admitted_runner_authority(outcome)
                claim = (
                    await self.input_admission_service
                    .begin_waiting_compatibility_apply(admission)
                )
                if claim is None:
                    return None
                batch, capability_snapshot = (
                    await self._resolve_batch_and_capability(
                        admission.input_batch_id,
                        session_id=admission.session_id,
                    )
                )
                # TODO(IR-4): remove compatibility semantic ownership after
                # CycleInputApplier applies every inbox item at safe checkpoints.
                result = await self.mcp_client.process_query(
                    "",
                    session_id=admission.session_id,
                    client_type=batch.client_type,
                    progress_callback=progress_callback,
                    progress_locale=progress_locale,
                    input_batch=batch,
                    cycle_id_override=cycle_id,
                )
                await (
                    self.input_admission_service
                    .complete_waiting_compatibility_apply(claim)
                )
                claim = None
                await self.input_admission_service.record_cycle_status(
                    session_id=admission.session_id,
                    cycle_id=cycle_id,
                    status=self._cycle_status_from_result(result),
                )
                await self._assemble_final_if_needed(
                    result=result,
                    batch=batch,
                    capability_snapshot=capability_snapshot,
                    progress_locale=progress_locale,
                )
                return result
        except APIError:
            raise
        except Exception as error:
            if claim is not None:
                try:
                    await (
                        self.input_admission_service
                        .requeue_waiting_compatibility_apply(
                            claim,
                            error_code="waiting_compatibility_failed",
                        )
                    )
                except Exception:
                    logger.exception("Failed to requeue compatibility claim")
            try:
                await self.input_admission_service.record_cycle_status(
                    session_id=admission.session_id,
                    cycle_id=cycle_id,
                    status=CycleStatus.INTERRUPTED,
                )
            except Exception:
                logger.exception("Failed to record compatibility interruption")
            raise APIError(f"Ошибка resume admitted cycle: {error}") from error

    async def _call_agent_batch_compatibility(
        self,
        input_batch_id: str,
        *,
        session_id: str,
        progress_callback=None,
        progress_locale: str = "ru",
    ) -> AgentResult:
        """Pre-IR-3 behavior used only when input_runtime.enabled is false."""
        batch, capability_snapshot = await self._resolve_batch_and_capability(
            input_batch_id,
            session_id=session_id,
        )
        async with self.execution_coordinator.run_lease(
            session_id=session_id,
            input_batch_id=input_batch_id,
            cycle_id=(cycle_id := uuid4().hex),
        ):
            result = await self.mcp_client.process_query(
                "",
                session_id=session_id,
                client_type=batch.client_type,
                progress_callback=progress_callback,
                progress_locale=progress_locale,
                input_batch=batch,
                cycle_id_override=cycle_id,
            )
            await self._assemble_final_if_needed(
                result=result,
                batch=batch,
                capability_snapshot=capability_snapshot,
                progress_locale=progress_locale,
            )
        return result

    async def call_agent_batch(
        self,
        input_batch_id: str,
        *,
        session_id: str,
        progress_callback=None,
        progress_locale: str = "ru",
    ) -> AgentResult:
        """Compatibility facade; enabled runtime always admits before execution."""
        try:
            if not self.input_runtime_config.enabled:
                return await self._call_agent_batch_compatibility(
                    input_batch_id,
                    session_id=session_id,
                    progress_callback=progress_callback,
                    progress_locale=progress_locale,
                )
            outcome = await self.admit_committed_batch(
                input_batch_id,
                session_id=session_id,
            )
            result = None
            if outcome.should_start_runner:
                result = await self.start_admitted_cycle(
                    outcome,
                    progress_callback=progress_callback,
                    progress_locale=progress_locale,
                )
            elif outcome.action in {
                InputAdmissionAction.RESUME_WAITING,
                InputAdmissionAction.DUPLICATE,
            } and outcome.should_wake_runner:
                result = await self.resume_admitted_cycle(
                    outcome,
                    progress_callback=progress_callback,
                    progress_locale=progress_locale,
                )
            if result is not None:
                return result
            return AgentResult(
                content=outcome.user_projection_key,
                status=AgentStatus.RUNNING,
                session_id=session_id,
                cycle_id=outcome.target_cycle_id,
            )
        except APIError:
            raise
        except Exception as error:
            logger.error("Ошибка запуска committed input batch: %r", error)
            raise APIError(
                f"Ошибка обработки committed input batch: {error}"
            ) from error

    async def call_agent(
        self,
        message: str,
        session_id: str = "default",
        client_type: ClientType | None = None,
        progress_callback=None,
        progress_locale: str = "ru",
    ) -> AgentResult:
        """Compatibility text-only entrypoint."""
        try:
            if not await self.mcp_client.list_tools():
                logger.warning("Нет зарегистрированных инструментов")
            async with self.execution_coordinator.run_lease(
                session_id=session_id,
                cycle_id=(cycle_id := uuid4().hex),
            ):
                result = await self.mcp_client.process_query(
                    message,
                    session_id=session_id,
                    client_type=client_type,
                    progress_callback=progress_callback,
                    progress_locale=progress_locale,
                    cycle_id_override=cycle_id,
                )
            logger.info("Ответ получен")
            return result
        except Exception as error:
            logger.error("Ошибка при обращении к MCP-клиенту: %s", error)
            state = None
            try:
                state = self.mcp_client.get_session_state(session_id)
            except Exception:
                pass
            return AgentResult(
                content=f"Ошибка при обработке запроса: {error}",
                status=AgentStatus.ERROR,
                session_id=session_id,
                iterations=state.iterations if state else 0,
                tools_used=state.tools_used if state else [],
                error=str(error),
                error_kind="critical_error",
                can_resume=False,
                progress_events=state.progress_events if state else [],
            )

    async def reset(self, session_id: str):
        self.mcp_client.clear_session(session_id)

    async def stop(self):
        await self.execution_coordinator.shutdown()
        try:
            await self.mcp_client.cleanup()
        except Exception as error:
            logger.error("Ошибка при отключении от сервера: %s", error)


API = Api(AGENT_CONFIG_PATH)
