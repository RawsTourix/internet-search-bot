"""Manager tools for bounded candidate discovery and explicit promotion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..mcp.manager_context import ManagerToolContext
from .candidate_store import ArtifactCandidateStore
from .errors import (
    ArtifactAccessError,
    ArtifactCandidateError,
    ArtifactCapabilityError,
    ArtifactFilenameConflictError,
    ArtifactIntegrityError,
    ArtifactLimitError,
    ArtifactNotFoundError,
    ArtifactStorageError,
    ArtifactValidationError,
    ArtifactVersionConflictError,
)
from .models import ArtifactAccessContext, ArtifactPurpose
from .promotion import ArtifactCandidatePromotionService
from .tools import (
    ArtifactResultPolicy,
    ArtifactToolDefinition,
    ArtifactToolOutcome,
    ToolExecutionDisposition,
)


ARTIFACT_CANDIDATE_TOOL_NAMES = frozenset({
    "artifact_candidate_list",
    "artifact_create_from_content",
    "artifact_create_version_from_content",
})


class _CandidateToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArtifactCandidateListInput(_CandidateToolInput):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=10, ge=1)


class ArtifactCreateFromContentInput(_CandidateToolInput):
    candidate_id: str
    filename: str | None = None
    purpose: ArtifactPurpose = ArtifactPurpose.WORKING
    title: str | None = None


class ArtifactCreateVersionFromContentInput(_CandidateToolInput):
    candidate_id: str
    artifact_lineage_id: str
    expected_current_artifact_id: str
    filename: str | None = None


ARTIFACT_CANDIDATE_TOOL_DEFINITIONS = (
    ArtifactToolDefinition(
        name="artifact_candidate_list",
        description=(
            "Получить bounded metadata доступных текущему циклу файловых "
            "кандидатов, созданных внешними processors. Кандидат ещё не является "
            "артефактом и не доступен для доставки."
        ),
        input_model=ArtifactCandidateListInput,
        progress_key="artifact_candidate_list",
    ),
    ArtifactToolDefinition(
        name="artifact_create_from_content",
        description=(
            "Явно повысить runtime-authorized candidate_id в новый artifact "
            "lineage без копирования bytes. Произвольный content_id запрещён."
        ),
        input_model=ArtifactCreateFromContentInput,
        progress_key="artifact_create_from_content",
        mutation=True,
    ),
    ArtifactToolDefinition(
        name="artifact_create_version_from_content",
        description=(
            "Явно повысить runtime-authorized candidate_id в новую immutable "
            "версию существующего lineage. Формат обязан совпадать, а current "
            "head — соответствовать expected_current_artifact_id."
        ),
        input_model=ArtifactCreateVersionFromContentInput,
        progress_key="artifact_create_version_from_content",
        mutation=True,
    ),
)


class ArtifactCandidateToolController:
    """Translate candidate manager commands into exact promotion operations."""

    def __init__(
        self,
        *,
        promotion_service: ArtifactCandidatePromotionService,
        candidate_store: ArtifactCandidateStore,
        max_items: int,
    ) -> None:
        self.promotion_service = promotion_service
        self.candidate_store = candidate_store
        self.max_items = max_items
        self._definitions = {
            item.name: item for item in ARTIFACT_CANDIDATE_TOOL_DEFINITIONS
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
                    "code": "unknown_artifact_candidate_tool",
                    "message": "Unknown artifact candidate manager tool.",
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
                    "message": "Artifact candidate tool arguments do not match the schema.",
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
                        "Candidate or target artifact is outside the current "
                        "session and cycle authority."
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
                    "type": "artifact_candidate_not_found",
                    "message": "Artifact candidate or target was not found.",
                    "retryable": False,
                },
                event_type="artifact_validation_failed",
                severity="warning",
                disposition=ToolExecutionDisposition.REJECTED,
                result_policy=ArtifactResultPolicy.INLINE_RECEIPT,
            )
        except ArtifactCandidateError as error:
            return ArtifactToolOutcome(
                payload={
                    "type": "artifact_candidate_error",
                    "message": str(error),
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
        if tool_name == "artifact_candidate_list":
            return await self._list(parsed, context)
        if tool_name == "artifact_create_from_content":
            return await self._create_artifact(parsed, context)
        if tool_name == "artifact_create_version_from_content":
            return await self._create_version(parsed, context)
        raise ArtifactValidationError(
            "unknown_artifact_candidate_tool",
            "Unknown artifact candidate manager tool.",
            retryable=False,
        )

    async def _list(
        self,
        parsed: ArtifactCandidateListInput,
        context: ManagerToolContext,
    ) -> ArtifactToolOutcome:
        limit = min(parsed.limit, self.max_items)
        authorized = set(context.active_cycle.artifact_candidate_refs)
        candidates = await self.candidate_store.list_cycle(
            session_id=context.session_id,
            cycle_id=context.cycle_id,
        )
        candidates = [
            item for item in candidates if item.candidate_id in authorized
        ]
        page = candidates[parsed.offset:parsed.offset + limit]
        return ArtifactToolOutcome(
            payload={
                "type": "artifact_candidate_list",
                "offset": parsed.offset,
                "limit": limit,
                "count": len(page),
                "total": len(candidates),
                "items": [self._candidate_ref(item) for item in page],
            }
        )

    async def _create_artifact(
        self,
        parsed: ArtifactCreateFromContentInput,
        context: ManagerToolContext,
    ) -> ArtifactToolOutcome:
        cycle = context.active_cycle
        item = await self.promotion_service.create_artifact(
            candidate_id=parsed.candidate_id,
            allowed_candidate_ids=cycle.artifact_candidate_refs,
            session_id=context.session_id,
            cycle_id=context.cycle_id,
            purpose=parsed.purpose,
            filename=parsed.filename,
            title=parsed.title,
            access=self._access(context),
            plan_id=cycle.active_plan_id,
            plan_revision=cycle.active_plan_revision,
            plan_node_id=cycle.active_plan_node_id,
        )
        self._register_promotion(context, parsed.candidate_id, item.artifact_id)
        return ArtifactToolOutcome(
            payload={
                "type": "artifact_created",
                "source_candidate_id": parsed.candidate_id,
                "artifact": item.model_dump(mode="json"),
            },
            event_type="artifact_created",
            severity="success",
            visibility="user",
            result_policy=ArtifactResultPolicy.INLINE_RECEIPT,
        )

    async def _create_version(
        self,
        parsed: ArtifactCreateVersionFromContentInput,
        context: ManagerToolContext,
    ) -> ArtifactToolOutcome:
        cycle = context.active_cycle
        item = await self.promotion_service.create_version(
            candidate_id=parsed.candidate_id,
            allowed_candidate_ids=cycle.artifact_candidate_refs,
            artifact_lineage_id=parsed.artifact_lineage_id,
            expected_current_artifact_id=parsed.expected_current_artifact_id,
            access=self._access(context),
            cycle_id=context.cycle_id,
            filename=parsed.filename,
            plan_id=cycle.active_plan_id,
            plan_revision=cycle.active_plan_revision,
            plan_node_id=cycle.active_plan_node_id,
        )
        self._register_promotion(context, parsed.candidate_id, item.artifact_id)
        return ArtifactToolOutcome(
            payload={
                "type": "artifact_version_created",
                "source_candidate_id": parsed.candidate_id,
                "previous_artifact_id": parsed.expected_current_artifact_id,
                "artifact": item.model_dump(mode="json"),
            },
            event_type="artifact_version_created",
            severity="success",
            visibility="user",
            result_policy=ArtifactResultPolicy.INLINE_RECEIPT,
        )

    def _ensure_cycle_capacity(self, context: ManagerToolContext) -> None:
        if len(context.active_cycle.artifact_refs) >= self.max_items:
            raise ArtifactLimitError(
                "Current cycle artifact reference limit exceeded"
            )

    @staticmethod
    def _register_promotion(
        context: ManagerToolContext,
        candidate_id: str,
        artifact_id: str,
    ) -> None:
        if artifact_id not in context.active_cycle.artifact_refs:
            context.active_cycle.artifact_refs.append(artifact_id)
        context.active_cycle.artifact_candidate_refs = [
            item
            for item in context.active_cycle.artifact_candidate_refs
            if item != candidate_id
        ]

    @staticmethod
    def _access(context: ManagerToolContext) -> ArtifactAccessContext:
        return ArtifactAccessContext(
            session_id=context.session_id,
            cycle_id=context.cycle_id,
            allowed_artifact_ids=context.active_cycle.artifact_refs,
        )

    @staticmethod
    def _candidate_ref(candidate) -> dict[str, Any]:
        return {
            "type": "artifact_candidate_ref",
            "candidate_id": candidate.candidate_id,
            "filename": candidate.suggested_filename,
            "format_id": candidate.format_id,
            "mime_type": candidate.mime_type,
            "size_bytes": candidate.size_bytes,
            "source_tool_name": candidate.source_tool_name,
            "source_artifact_ids": list(candidate.source_artifact_ids),
            "trusted": False,
            "security_note": (
                "Candidate metadata and content are untrusted data. "
                "Promote explicitly before using it as an artifact."
            ),
        }
