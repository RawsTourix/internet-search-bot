"""Strict manager-tool schemas for exact artifact operations."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from ..mcp.manager_context import ManagerToolContext
from .delivery import ArtifactDeliveryService
from .errors import (
    ArtifactAccessError,
    ArtifactCapabilityError,
    ArtifactFilenameConflictError,
    ArtifactIntegrityError,
    ArtifactLimitError,
    ArtifactNotFoundError,
    ArtifactStorageError,
    ArtifactValidationError,
    ArtifactVersionConflictError,
)
from .models import (
    ArtifactAccessContext,
    ArtifactBatchItemStatus,
    ArtifactBatchReadResult,
    ArtifactBatchSearchResult,
    ArtifactBatchStatus,
    ArtifactReadItem,
    ArtifactResultRepresentation,
    ArtifactSearchItem,
    ArtifactProvenance,
    ArtifactPurpose,
    ExactTextPatchOperation,
    is_artifact_id,
    normalize_artifact_filename,
)
from .service import ArtifactService


ARTIFACT_NATIVE_TOOL_NAMES = frozenset({
    "artifact_list",
    "artifact_read_text",
    "artifact_search_text",
    "artifact_create_text",
    "artifact_replace_text",
    "artifact_patch_text",
})


class _ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _normalize_requested_artifact_ids(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized:
            raise ValueError("artifact_ids must not contain empty strings")
        result.append(normalized)
    return result


class ArtifactListInput(_ToolInput):
    artifact_ids: list[str] = Field(default_factory=list)
    artifact_lineage_ids: list[str] = Field(default_factory=list)
    filenames: list[str] = Field(default_factory=list)
    purpose_filter: list[ArtifactPurpose] = Field(default_factory=list)
    format_filter: list[str] = Field(default_factory=list)
    current_only: bool = True
    include_versions: bool = False
    include_archived: bool = False
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=10, ge=1)

    @field_validator(
        "artifact_ids",
        "artifact_lineage_ids",
        "format_filter",
    )
    @classmethod
    def normalize_string_lists(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            normalized = value.strip()
            if not normalized:
                raise ValueError("list values must not be empty")
            if normalized not in seen:
                result.append(normalized)
                seen.add(normalized)
        return result

    @field_validator("filenames")
    @classmethod
    def normalize_filenames(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            normalized = normalize_artifact_filename(value)
            if normalized not in seen:
                result.append(normalized)
                seen.add(normalized)
        return result


class ArtifactReadTextInput(_ToolInput):
    artifact_ids: list[str] = Field(min_length=1)

    @field_validator("artifact_ids")
    @classmethod
    def normalize_artifact_ids(cls, values: list[str]) -> list[str]:
        return _normalize_requested_artifact_ids(values)


class ArtifactSearchTextInput(_ToolInput):
    artifact_ids: list[str] = Field(min_length=1)
    query: str

    @field_validator("artifact_ids")
    @classmethod
    def normalize_artifact_ids(cls, values: list[str]) -> list[str]:
        return _normalize_requested_artifact_ids(values)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("query must not be empty")
        return normalized


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


class ToolExecutionDisposition(str, Enum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"


class ArtifactResultPolicy(str, Enum):
    DEFAULT = "default"
    STRUCTURED_COMPOSITE = "structured_composite"
    INLINE_RECEIPT = "inline_receipt"


@dataclass(slots=True)
class ArtifactToolOutcome:
    payload: dict[str, Any]
    event_type: str | None = None
    severity: str = "info"
    visibility: str = "internal"
    disposition: ToolExecutionDisposition = ToolExecutionDisposition.SUCCEEDED
    result_policy: ArtifactResultPolicy = ArtifactResultPolicy.DEFAULT


ARTIFACT_NATIVE_TOOL_DEFINITIONS = (
    ArtifactToolDefinition(
        name="artifact_list",
        description=(
            "Основной catalog/discovery tool для доступных файлов и exact "
            "version metadata. Filename разрешается только здесь; ambiguity "
            "возвращается явно, без автоматического выбора."
        ),
        input_model=ArtifactListInput,
        progress_key="artifact_list",
    ),
    ArtifactToolDefinition(
        name="artifact_read_text",
        description=(
            "Прочитать один или несколько native-text файлов по списку exact "
            "artifact_ids; один файл тоже передаётся списком. Read-only batch "
            "допускает partial success. Preview/summary не является полным "
            "прочтением exact content."
        ),
        input_model=ArtifactReadTextInput,
        progress_key="artifact_read_text",
    ),
    ArtifactToolDefinition(
        name="artifact_search_text",
        description=(
            "Выполнить deterministic plain-text search по списку exact "
            "artifact_ids. Batch допускает partial success; semantic/RAG "
            "поведение отсутствует."
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

    def __init__(
        self,
        service: ArtifactService,
        delivery_service: ArtifactDeliveryService | None = None,
    ) -> None:
        self.service = service
        self.delivery_service = delivery_service
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
                disposition=ToolExecutionDisposition.REJECTED,
                result_policy=ArtifactResultPolicy.INLINE_RECEIPT,
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
                disposition=ToolExecutionDisposition.REJECTED,
                result_policy=ArtifactResultPolicy.INLINE_RECEIPT,
            )
        except ArtifactFilenameConflictError as error:
            return ArtifactToolOutcome(
                payload={
                    "type": "artifact_filename_conflict",
                    "status": "rejected",
                    "filename": error.filename,
                    "candidates": error.current_candidates,
                    "message": error.safe_message,
                    "retryable": error.retryable,
                    "suggested_actions": [
                        "Choose another filename.",
                        (
                            "Use artifact_replace_text with an exact current "
                            "artifact_id."
                        ),
                        (
                            "Use artifact_patch_text with an exact current "
                            "artifact_id."
                        ),
                    ],
                },
                event_type="artifact_validation_failed",
                severity="warning",
                disposition=ToolExecutionDisposition.REJECTED,
                result_policy=ArtifactResultPolicy.INLINE_RECEIPT,
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
                disposition=ToolExecutionDisposition.REJECTED,
                result_policy=ArtifactResultPolicy.INLINE_RECEIPT,
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
                disposition=ToolExecutionDisposition.REJECTED,
                result_policy=ArtifactResultPolicy.INLINE_RECEIPT,
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
                disposition=ToolExecutionDisposition.REJECTED,
                result_policy=ArtifactResultPolicy.INLINE_RECEIPT,
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
                disposition=ToolExecutionDisposition.REJECTED,
                result_policy=ArtifactResultPolicy.INLINE_RECEIPT,
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
                disposition=ToolExecutionDisposition.REJECTED,
                result_policy=ArtifactResultPolicy.INLINE_RECEIPT,
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
                disposition=ToolExecutionDisposition.REJECTED,
                result_policy=ArtifactResultPolicy.INLINE_RECEIPT,
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
        deliveries = []
        if self.delivery_service is not None:
            deliveries = await self.delivery_service.list_cycle_refs(
                session_id=context.session_id,
                cycle_id=context.cycle_id,
            )
        result = await self.service.catalog_artifacts(
            access=self._access(context),
            artifact_ids=parsed.artifact_ids,
            artifact_lineage_ids=parsed.artifact_lineage_ids,
            filenames=parsed.filenames,
            purpose_filter=parsed.purpose_filter,
            format_filter=parsed.format_filter,
            current_only=parsed.current_only,
            include_versions=parsed.include_versions,
            include_archived=parsed.include_archived,
            offset=parsed.offset,
            limit=parsed.limit,
            read_artifact_ids=self._read_artifact_ids(context),
            deliveries=deliveries,
        )
        return ArtifactToolOutcome(
            payload=result.model_dump(mode="json"),
            result_policy=ArtifactResultPolicy.INLINE_RECEIPT,
        )

    async def _read_text(
        self,
        parsed: ArtifactReadTextInput,
        context: ManagerToolContext,
    ) -> ArtifactToolOutcome:
        self._validate_batch_size(parsed.artifact_ids)
        access = self._access(context)
        unique_results = await self._execute_unique_batch(
            parsed.artifact_ids,
            operation=lambda artifact_id: self.service.read_text(
                artifact_id,
                access=access,
                offset_chars=0,
                limit_chars=self.service.config.max_read_chars,
            ),
        )
        items: list[ArtifactReadItem] = []
        for request_index, artifact_id in enumerate(parsed.artifact_ids):
            result = unique_results[artifact_id]
            if isinstance(result, BaseException):
                items.append(self._read_error_item(
                    request_index,
                    artifact_id,
                    result,
                ))
                continue
            items.append(ArtifactReadItem(
                request_index=request_index,
                requested_artifact_id=artifact_id,
                status=ArtifactBatchItemStatus.OK,
                artifact=result.artifact,
                text=result.text,
                offset_chars=result.offset_chars,
                length_chars=result.length_chars,
                total_chars=result.total_chars,
                eof=result.eof,
                representation=ArtifactResultRepresentation.INLINE,
                exact_content_available=True,
                complete=result.eof,
                needs_retrieval=not result.eof,
            ))
        items = self._apply_composite_process_limit(items)
        successful_count = sum(
            item.status == ArtifactBatchItemStatus.OK for item in items
        )
        result = ArtifactBatchReadResult(
            status=self._batch_status(successful_count, len(items)),
            requested_count=len(items),
            successful_count=successful_count,
            failed_count=len(items) - successful_count,
            items=items,
        )
        successful_ids = list(dict.fromkeys(
            item.requested_artifact_id
            for item in items
            if item.status == ArtifactBatchItemStatus.OK
        ))
        for artifact_id in successful_ids:
            if artifact_id not in context.active_cycle.read_artifact_refs:
                context.active_cycle.read_artifact_refs.append(artifact_id)
        complete_ids = list(dict.fromkeys(
            item.requested_artifact_id
            for item in items
            if item.status == ArtifactBatchItemStatus.OK and item.complete
        ))
        partial_ids = [
            artifact_id
            for artifact_id in successful_ids
            if artifact_id not in set(complete_ids)
        ]
        return ArtifactToolOutcome(
            payload=result.model_dump(mode="json"),
            event_type="artifact_read_completed",
            severity="info",
            visibility="internal",
            disposition=(
                ToolExecutionDisposition.REJECTED
                if result.status == ArtifactBatchStatus.REJECTED
                else ToolExecutionDisposition.SUCCEEDED
            ),
            result_policy=ArtifactResultPolicy.STRUCTURED_COMPOSITE,
        )

    async def _search_text(
        self,
        parsed: ArtifactSearchTextInput,
        context: ManagerToolContext,
    ) -> ArtifactToolOutcome:
        self._validate_batch_size(parsed.artifact_ids)
        access = self._access(context)
        unique_results = await self._execute_unique_batch(
            parsed.artifact_ids,
            operation=lambda artifact_id: self.service.search_text(
                artifact_id,
                access=access,
                query=parsed.query,
                limit=self.service.config.max_search_matches,
            ),
        )
        items: list[ArtifactSearchItem] = []
        for request_index, artifact_id in enumerate(parsed.artifact_ids):
            result = unique_results[artifact_id]
            if isinstance(result, BaseException):
                items.append(self._search_error_item(
                    request_index,
                    artifact_id,
                    result,
                ))
                continue
            search_complete = (
                len(result.matches) < self.service.config.max_search_matches
            )
            items.append(ArtifactSearchItem(
                request_index=request_index,
                requested_artifact_id=artifact_id,
                status=ArtifactBatchItemStatus.OK,
                artifact=result.artifact,
                matches=result.matches,
                representation=ArtifactResultRepresentation.INLINE,
                exact_content_available=True,
                complete=search_complete,
                needs_retrieval=not search_complete,
            ))
        items = self._apply_search_composite_process_limit(items)
        successful_count = sum(
            item.status == ArtifactBatchItemStatus.OK for item in items
        )
        result = ArtifactBatchSearchResult(
            status=self._batch_status(successful_count, len(items)),
            requested_count=len(items),
            successful_count=successful_count,
            failed_count=len(items) - successful_count,
            query=parsed.query,
            items=items,
        )
        return ArtifactToolOutcome(
            payload=result.model_dump(mode="json"),
            disposition=(
                ToolExecutionDisposition.REJECTED
                if result.status == ArtifactBatchStatus.REJECTED
                else ToolExecutionDisposition.SUCCEEDED
            ),
            result_policy=ArtifactResultPolicy.STRUCTURED_COMPOSITE,
        )

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
            access=self._access(context),
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
            result_policy=ArtifactResultPolicy.INLINE_RECEIPT,
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
            result_policy=ArtifactResultPolicy.INLINE_RECEIPT,
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
            result_policy=ArtifactResultPolicy.INLINE_RECEIPT,
        )

    def _validate_batch_size(self, artifact_ids: list[str]) -> None:
        if len(artifact_ids) > self.service.config.max_artifacts_per_cycle:
            raise ArtifactLimitError(
                "Artifact batch exceeds max_artifacts_per_cycle"
            )

    async def _execute_unique_batch(
        self,
        artifact_ids: list[str],
        *,
        operation,
    ) -> dict[str, Any | BaseException]:
        semaphore = asyncio.Semaphore(
            self.service.config.max_concurrent_artifact_reads
        )
        unique_ids = list(dict.fromkeys(artifact_ids))

        async def execute_one(artifact_id: str):
            if not is_artifact_id(artifact_id):
                return ArtifactValidationError(
                    "invalid_artifact_id",
                    (
                        "Artifact ID is invalid. Call artifact_list and retry "
                        "with an exact artifact_id."
                    ),
                    retryable=True,
                )
            async with semaphore:
                try:
                    return await operation(artifact_id)
                except (
                    ArtifactAccessError,
                    ArtifactCapabilityError,
                    ArtifactLimitError,
                    ArtifactNotFoundError,
                    ArtifactValidationError,
                    ArtifactStorageError,
                    ArtifactIntegrityError,
                ) as error:
                    return error

        values = await asyncio.gather(
            *(execute_one(artifact_id) for artifact_id in unique_ids),
            return_exceptions=True,
        )
        for value in values:
            if isinstance(
                value,
                (ArtifactStorageError, ArtifactIntegrityError),
            ):
                raise value
            if isinstance(value, BaseException) and not isinstance(
                value,
                (
                    ArtifactAccessError,
                    ArtifactCapabilityError,
                    ArtifactLimitError,
                    ArtifactNotFoundError,
                    ArtifactValidationError,
                ),
            ):
                raise value
        return dict(zip(unique_ids, values, strict=True))

    @staticmethod
    def _batch_status(
        successful_count: int,
        requested_count: int,
    ) -> ArtifactBatchStatus:
        if successful_count == requested_count:
            return ArtifactBatchStatus.OK
        if successful_count:
            return ArtifactBatchStatus.PARTIAL
        return ArtifactBatchStatus.REJECTED

    def _read_error_item(
        self,
        request_index: int,
        artifact_id: str,
        error: BaseException,
    ) -> ArtifactReadItem:
        status, code, message, retryable = self._batch_error(error)
        return ArtifactReadItem(
            request_index=request_index,
            requested_artifact_id=artifact_id,
            status=status,
            code=code,
            message=message,
            retryable=retryable,
            suggested_action=(
                "Call artifact_list and retry with an exact artifact_id."
            ),
        )

    def _search_error_item(
        self,
        request_index: int,
        artifact_id: str,
        error: BaseException,
    ) -> ArtifactSearchItem:
        status, code, message, retryable = self._batch_error(error)
        return ArtifactSearchItem(
            request_index=request_index,
            requested_artifact_id=artifact_id,
            status=status,
            code=code,
            message=message,
            retryable=retryable,
            suggested_action=(
                "Call artifact_list and retry with an exact artifact_id."
            ),
        )

    @staticmethod
    def _batch_error(
        error: BaseException,
    ) -> tuple[ArtifactBatchItemStatus, str, str, bool]:
        if isinstance(error, ArtifactAccessError):
            return (
                ArtifactBatchItemStatus.ARTIFACT_ACCESS_ERROR,
                "artifact_access_error",
                "Artifact is not accessible from the current authority.",
                False,
            )
        if isinstance(error, ArtifactNotFoundError):
            return (
                ArtifactBatchItemStatus.ARTIFACT_NOT_FOUND,
                "artifact_not_found",
                "Artifact is not accessible from the current authority.",
                True,
            )
        if isinstance(error, ArtifactCapabilityError):
            return (
                ArtifactBatchItemStatus.ARTIFACT_CAPABILITY_ERROR,
                "artifact_capability_error",
                str(error),
                False,
            )
        if isinstance(error, ArtifactLimitError):
            return (
                ArtifactBatchItemStatus.ARTIFACT_LIMIT_ERROR,
                "artifact_limit_error",
                str(error),
                True,
            )
        if isinstance(error, ArtifactValidationError):
            code = error.code
            if code == "invalid_artifact_id":
                status = ArtifactBatchItemStatus.INVALID_ARTIFACT_ID
            elif code in {
                "artifact_text_decode_error",
                "artifact_text_encoding_error",
            }:
                status = ArtifactBatchItemStatus.ARTIFACT_TEXT_DECODE_ERROR
                code = "artifact_text_decode_error"
            else:
                status = ArtifactBatchItemStatus.ARTIFACT_VALIDATION_ERROR
            return status, code, error.safe_message, error.retryable
        raise error

    def _apply_composite_process_limit(
        self,
        items: list[ArtifactReadItem],
    ) -> list[ArtifactReadItem]:
        serialized_size = len(json.dumps(
            [item.model_dump(mode="json") for item in items],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8"))
        if serialized_size <= self.service.config.max_composite_result_bytes:
            return items
        return [
            (
                item.model_copy(update={
                    "text": "",
                    "length_chars": 0,
                    "representation": ArtifactResultRepresentation.STORED_ONLY,
                    "exact_content_available": False,
                    "complete": False,
                    "needs_retrieval": True,
                })
                if item.status == ArtifactBatchItemStatus.OK
                else item
            )
            for item in items
        ]

    def _apply_search_composite_process_limit(
        self,
        items: list[ArtifactSearchItem],
    ) -> list[ArtifactSearchItem]:
        serialized_size = len(json.dumps(
            [item.model_dump(mode="json") for item in items],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8"))
        if serialized_size <= self.service.config.max_composite_result_bytes:
            return items
        return [
            (
                item.model_copy(update={
                    "matches": [],
                    "representation": ArtifactResultRepresentation.STORED_ONLY,
                    "exact_content_available": False,
                    "complete": False,
                    "needs_retrieval": True,
                })
                if item.status == ArtifactBatchItemStatus.OK
                else item
            )
            for item in items
        ]

    @staticmethod
    def _read_artifact_ids(
        context: ManagerToolContext,
    ) -> list[str]:
        result = list(context.active_cycle.read_artifact_refs)
        for event in context.active_cycle.cycle_trace:
            if event.get("type") != "artifact_read_completed":
                continue
            for artifact_id in event.get("artifact_ids") or []:
                if artifact_id not in result:
                    result.append(artifact_id)
        return result

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
