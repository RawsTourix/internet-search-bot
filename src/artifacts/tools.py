"""Strict manager-tool schemas for exact artifact operations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..mcp.manager_context import ManagerToolContext
from .errors import (
    ArtifactAccessError,
    ArtifactCapabilityError,
    ArtifactIntegrityError,
    ArtifactLimitError,
    ArtifactNotFoundError,
    ArtifactStorageError,
    ArtifactValidationError,
    ArtifactVersionConflictError,
)
from .models import (
    ArtifactAccessContext,
    ArtifactProvenance,
    ArtifactPurpose,
    ExactTextPatchOperation,
)
from .service import ArtifactService


ARTIFACT_NATIVE_TOOL_NAMES = frozenset({
    "artifact_list",
    "artifact_get",
    "artifact_read_text",
    "artifact_search_text",
    "artifact_create_text",
    "artifact_replace_text",
    "artifact_patch_text",
})


class _ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArtifactListInput(_ToolInput):
    purpose_filter: list[ArtifactPurpose] = Field(default_factory=list)
    format_filter: list[str] = Field(default_factory=list)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=10, ge=1)
    include_archived: bool = False


class ArtifactGetInput(_ToolInput):
    artifact_id: str


class ArtifactReadTextInput(_ToolInput):
    artifact_id: str
    offset_chars: int = Field(default=0, ge=0)
    limit_chars: int = Field(default=20_000, ge=1)


class ArtifactSearchTextInput(_ToolInput):
    artifact_id: str
    query: str
    limit: int = Field(default=10, ge=1)


class ArtifactCreateTextInput(_ToolInput):
    filename: str
    text: str
    format_id: str = "markdown"
    purpose: ArtifactPurpose = ArtifactPurpose.WORKING
    title: str | None = None


class ArtifactReplaceTextInput(_ToolInput):
    artifact_id: str
    expected_current_artifact_id: str
    new_text: str
    filename: str | None = None


class ArtifactPatchTextInput(_ToolInput):
    artifact_id: str
    expected_current_artifact_id: str
    operations: list[ExactTextPatchOperation]
    filename: str | None = None


@dataclass(frozen=True, slots=True)
class ArtifactToolDefinition:
    name: str
    description: str
    input_model: type[BaseModel]
    progress_key: str
    mutation: bool = False

    def parameters(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()


@dataclass(slots=True)
class ArtifactToolOutcome:
    payload: dict[str, Any]
    event_type: str | None = None
    severity: str = "info"
    visibility: str = "internal"


ARTIFACT_NATIVE_TOOL_DEFINITIONS = (
    ArtifactToolDefinition(
        name="artifact_list",
        description=(
            "Получить компактный список доступных текущему циклу файлов. "
            "Возвращает только точные runtime-authorized версии и metadata."
        ),
        input_model=ArtifactListInput,
        progress_key="artifact_list",
    ),
    ArtifactToolDefinition(
        name="artifact_get",
        description=(
            "Точно получить metadata одной immutable версии файла по artifact_id. "
            "Инструмент не читает содержимое файла."
        ),
        input_model=ArtifactGetInput,
        progress_key="artifact_get",
    ),
    ArtifactToolDefinition(
        name="artifact_read_text",
        description=(
            "Прочитать ограниченный символьный диапазон native-text артефакта. "
            "Для PDF/DOCX/XLSX/PPTX нужен внешний processor."
        ),
        input_model=ArtifactReadTextInput,
        progress_key="artifact_read_text",
    ),
    ArtifactToolDefinition(
        name="artifact_search_text",
        description=(
            "Выполнить точный последовательный поиск строки в одной известной "
            "native-text версии без RAG и semantic search."
        ),
        input_model=ArtifactSearchTextInput,
        progress_key="artifact_search_text",
    ),
    ArtifactToolDefinition(
        name="artifact_create_text",
        description=(
            "Создать новый native-text файл и первую immutable версию. "
            "Runtime создаёт opaque IDs и provenance."
        ),
        input_model=ArtifactCreateTextInput,
        progress_key="artifact_create_text",
        mutation=True,
    ),
    ArtifactToolDefinition(
        name="artifact_replace_text",
        description=(
            "Полностью заменить текст current версии и создать новую immutable "
            "версию. Требует актуальный expected_current_artifact_id."
        ),
        input_model=ArtifactReplaceTextInput,
        progress_key="artifact_replace_text",
        mutation=True,
    ),
    ArtifactToolDefinition(
        name="artifact_patch_text",
        description=(
            "Применить детерминированные exact replacements и создать новую "
            "immutable версию. Fuzzy matching и частичные изменения запрещены."
        ),
        input_model=ArtifactPatchTextInput,
        progress_key="artifact_patch_text",
        mutation=True,
    ),
)


class ArtifactToolController:
    """Translate manager commands into ArtifactService calls and safe payloads."""

    def __init__(self, service: ArtifactService) -> None:
        self.service = service
        self._definitions = {
            item.name: item for item in ARTIFACT_NATIVE_TOOL_DEFINITIONS
        }

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ManagerToolContext,
    ) -> ArtifactToolOutcome:
        definition = self._definitions.get(tool_name)
        if definition is None:
            return ArtifactToolOutcome(
                payload={
                    "type": "artifact_validation_error",
                    "code": "unknown_artifact_tool",
                    "message": "Unknown artifact manager tool.",
                    "retryable": False,
                },
                event_type="artifact_validation_failed",
                severity="error",
            )
        try:
            parsed = definition.input_model.model_validate(arguments)
            if definition.mutation:
                self._ensure_cycle_capacity(context)
            return await self._dispatch(tool_name, parsed, context)
        except ValidationError as error:
            return ArtifactToolOutcome(
                payload={
                    "type": "artifact_validation_error",
                    "code": "invalid_tool_arguments",
                    "message": "Artifact tool arguments do not match the schema.",
                    "retryable": True,
                    "details": {"issue_count": error.error_count()},
                },
                event_type="artifact_validation_failed",
                severity="warning",
            )
        except ArtifactVersionConflictError as error:
            return ArtifactToolOutcome(
                payload={
                    "type": "artifact_version_conflict",
                    "artifact_lineage_id": error.artifact_lineage_id,
                    "expected_current_artifact_id": (
                        error.expected_current_artifact_id
                    ),
                    "current_artifact_id": error.current_artifact_id,
                    "current_version": error.current_version,
                    "current_artifact": error.current_ref,
                    "retryable": True,
                },
                event_type="artifact_version_conflict",
                severity="warning",
                visibility="user",
            )
        except ArtifactValidationError as error:
            return ArtifactToolOutcome(
                payload={
                    "type": "artifact_validation_error",
                    "code": error.code,
                    "message": error.safe_message,
                    "retryable": error.retryable,
                    "details": error.details,
                },
                event_type="artifact_validation_failed",
                severity="warning",
            )
        except ArtifactAccessError:
            return ArtifactToolOutcome(
                payload={
                    "type": "artifact_access_error",
                    "message": (
                        "Artifact is not accessible from the current session "
                        "and cycle authority."
                    ),
                    "retryable": False,
                },
                event_type="artifact_validation_failed",
                severity="error",
            )
        except ArtifactNotFoundError:
            return ArtifactToolOutcome(
                payload={
                    "type": "artifact_not_found",
                    "message": "Artifact was not found.",
                    "retryable": False,
                },
                event_type="artifact_validation_failed",
                severity="warning",
            )
        except ArtifactCapabilityError as error:
            return ArtifactToolOutcome(
                payload={
                    "type": "artifact_capability_error",
                    "message": str(error),
                    "retryable": False,
                },
                event_type="artifact_validation_failed",
                severity="warning",
            )
        except ArtifactLimitError as error:
            return ArtifactToolOutcome(
                payload={
                    "type": "artifact_limit_error",
                    "message": str(error),
                    "retryable": True,
                },
                event_type="artifact_validation_failed",
                severity="warning",
            )
        except (ArtifactStorageError, ArtifactIntegrityError):
            raise

    async def _dispatch(
        self,
        tool_name: str,
        parsed: BaseModel,
        context: ManagerToolContext,
    ) -> ArtifactToolOutcome:
        if tool_name == "artifact_list":
            return await self._list(parsed, context)
        if tool_name == "artifact_get":
            return await self._get(parsed, context)
        if tool_name == "artifact_read_text":
            return await self._read_text(parsed, context)
        if tool_name == "artifact_search_text":
            return await self._search_text(parsed, context)
        if tool_name == "artifact_create_text":
            return await self._create_text(parsed, context)
        if tool_name == "artifact_replace_text":
            return await self._replace_text(parsed, context)
        if tool_name == "artifact_patch_text":
            return await self._patch_text(parsed, context)
        raise ArtifactValidationError(
            "unknown_artifact_tool",
            "Unknown artifact manager tool.",
            retryable=False,
        )

    async def _list(
        self,
        parsed: ArtifactListInput,
        context: ManagerToolContext,
    ) -> ArtifactToolOutcome:
        items = await self.service.list_artifacts(
            access=self._access(context),
            purpose_filter=parsed.purpose_filter,
            format_filter=parsed.format_filter,
            offset=parsed.offset,
            limit=parsed.limit,
            include_archived=parsed.include_archived,
        )
        return ArtifactToolOutcome(
            payload={
                "type": "artifact_list",
                "offset": parsed.offset,
                "limit": min(
                    parsed.limit,
                    self.service.config.max_artifacts_per_cycle,
                ),
                "count": len(items),
                "items": [item.model_dump(mode="json") for item in items],
            }
        )

    async def _get(
        self,
        parsed: ArtifactGetInput,
        context: ManagerToolContext,
    ) -> ArtifactToolOutcome:
        item = await self.service.get_artifact(
            parsed.artifact_id,
            access=self._access(context),
        )
        return ArtifactToolOutcome(
            payload={
                "type": "artifact_metadata",
                "artifact": item.model_dump(mode="json"),
            }
        )

    async def _read_text(
        self,
        parsed: ArtifactReadTextInput,
        context: ManagerToolContext,
    ) -> ArtifactToolOutcome:
        result = await self.service.read_text(
            parsed.artifact_id,
            access=self._access(context),
            offset_chars=parsed.offset_chars,
            limit_chars=parsed.limit_chars,
        )
        return ArtifactToolOutcome(payload=result.model_dump(mode="json"))

    async def _search_text(
        self,
        parsed: ArtifactSearchTextInput,
        context: ManagerToolContext,
    ) -> ArtifactToolOutcome:
        result = await self.service.search_text(
            parsed.artifact_id,
            access=self._access(context),
            query=parsed.query,
            limit=parsed.limit,
        )
        return ArtifactToolOutcome(payload=result.model_dump(mode="json"))

    async def _create_text(
        self,
        parsed: ArtifactCreateTextInput,
        context: ManagerToolContext,
    ) -> ArtifactToolOutcome:
        item = await self.service.create_text(
            session_id=context.session_id,
            cycle_id=context.cycle_id,
            filename=parsed.filename,
            text=parsed.text,
            format_id=parsed.format_id,
            purpose=parsed.purpose,
            title=parsed.title,
            provenance=self._provenance(
                context,
                origin="agent_created",
                operation="create_text",
            ),
        )
        self._register_ref(context, item.artifact_id)
        return ArtifactToolOutcome(
            payload={
                "type": "artifact_created",
                "artifact": item.model_dump(mode="json"),
            },
            event_type="artifact_created",
            severity="success",
            visibility="user",
        )

    async def _replace_text(
        self,
        parsed: ArtifactReplaceTextInput,
        context: ManagerToolContext,
    ) -> ArtifactToolOutcome:
        item = await self.service.replace_text(
            artifact_id=parsed.artifact_id,
            expected_current_artifact_id=parsed.expected_current_artifact_id,
            access=self._access(context),
            cycle_id=context.cycle_id,
            new_text=parsed.new_text,
            filename=parsed.filename,
            provenance=self._provenance(
                context,
                origin="agent_edit",
                operation="replace_text",
                source_artifact_ids=[parsed.artifact_id],
            ),
        )
        self._register_ref(context, item.artifact_id)
        return ArtifactToolOutcome(
            payload={
                "type": "artifact_version_created",
                "artifact": item.model_dump(mode="json"),
                "previous_artifact_id": parsed.expected_current_artifact_id,
            },
            event_type="artifact_version_created",
            severity="success",
            visibility="user",
        )

    async def _patch_text(
        self,
        parsed: ArtifactPatchTextInput,
        context: ManagerToolContext,
    ) -> ArtifactToolOutcome:
        item = await self.service.patch_text(
            artifact_id=parsed.artifact_id,
            expected_current_artifact_id=parsed.expected_current_artifact_id,
            access=self._access(context),
            cycle_id=context.cycle_id,
            operations=parsed.operations,
            filename=parsed.filename,
            provenance=self._provenance(
                context,
                origin="agent_edit",
                operation="patch_text",
                source_artifact_ids=[parsed.artifact_id],
            ),
        )
        self._register_ref(context, item.artifact_id)
        return ArtifactToolOutcome(
            payload={
                "type": "artifact_version_created",
                "artifact": item.model_dump(mode="json"),
                "previous_artifact_id": parsed.expected_current_artifact_id,
                "patch_operation_count": len(parsed.operations),
            },
            event_type="artifact_version_created",
            severity="success",
            visibility="user",
        )

    def _ensure_cycle_capacity(self, context: ManagerToolContext) -> None:
        if (
            len(context.active_cycle.artifact_refs)
            >= self.service.config.max_artifacts_per_cycle
        ):
            raise ArtifactLimitError(
                "Current cycle artifact reference limit exceeded"
            )

    @staticmethod
    def _register_ref(
        context: ManagerToolContext,
        artifact_id: str,
    ) -> None:
        if artifact_id not in context.active_cycle.artifact_refs:
            context.active_cycle.artifact_refs.append(artifact_id)

    @staticmethod
    def _access(context: ManagerToolContext) -> ArtifactAccessContext:
        return ArtifactAccessContext(
            session_id=context.session_id,
            cycle_id=context.cycle_id,
            allowed_artifact_ids=context.active_cycle.artifact_refs,
        )

    @staticmethod
    def _provenance(
        context: ManagerToolContext,
        *,
        origin: str,
        operation: str,
        source_artifact_ids: list[str] | None = None,
    ) -> ArtifactProvenance:
        cycle = context.active_cycle
        return ArtifactProvenance(
            origin=origin,
            creator="agent",
            source_artifact_ids=list(source_artifact_ids or []),
            plan_id=cycle.active_plan_id,
            plan_revision=cycle.active_plan_revision,
            plan_node_id=cycle.active_plan_node_id,
            operation=operation,
        )
