import logging
import os
from collections.abc import AsyncIterator, Mapping
from logging.handlers import RotatingFileHandler
from pathlib import Path

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
from ..mcp.artifact_delivery_runtime import (
    FinalizingArtifactDeliveryPlanningMCPClient,
)
from ..mcp.mcp_client import load_config
from ..planning import create_planning_services, load_planning_config
from ..planning.runtime_context import PlanningAwareContentStore
from ..storage import StorageServices, create_storage_services


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
            logger.info(
                "Загрузка конфигурации MCP, LLM, storage, memory, runtime, "
                "planning, artifacts и ingress"
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
        """Persist a client envelope using the shared semantic grouping policy."""
        try:
            grouping = resolve_input_grouping(envelope)
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

    async def call_agent_batch(
        self,
        input_batch_id: str,
        *,
        session_id: str,
        progress_callback=None,
        progress_locale: str = "ru",
    ) -> AgentResult:
        """Run the agent only from an authoritative committed input batch."""
        try:
            batch = await self.ingress_services.batch_store.get_committed(
                input_batch_id
            )
            if batch.session_id != session_id:
                raise APIError("Input batch belongs to another session")
            return await self.mcp_client.process_query(
                "",
                session_id=session_id,
                client_type=batch.client_type,
                progress_callback=progress_callback,
                progress_locale=progress_locale,
                input_batch=batch,
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
            result = await self.mcp_client.process_query(
                message,
                session_id=session_id,
                client_type=client_type,
                progress_callback=progress_callback,
                progress_locale=progress_locale,
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
        try:
            await self.mcp_client.cleanup()
        except Exception as error:
            logger.error("Ошибка при отключении от сервера: %s", error)


API = Api(AGENT_CONFIG_PATH)
