"""Strict manager-tool schemas and command controller for DAG plans."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ..mcp.manager_context import ManagerToolContext
from .errors import (
    PlanAccessError,
    PlanNotFoundError,
    PlanRevisionConflictError,
    PlanStorageError,
    PlanValidationError,
)
from .models import (
    ActivePlanState,
    AddPlanNodeInput,
    CreatePlanNodeInput,
    PlanNodeKind,
    PlanNodeStatus,
    PlanNodeTransition,
    PlanStatus,
)
from .service import PlanningService


PLAN_TOOL_NAMES = frozenset({
    "agent_plan_create",
    "agent_plan_get",
    "agent_plan_add_nodes",
    "agent_plan_update_node",
    "agent_plan_transition_node",
    "agent_plan_remove_node",
    "agent_plan_cancel",
})


class _ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanGetView(str, Enum):
    SUMMARY = "summary"
    NODES = "nodes"
    NODE = "node"


class CreatePlanInput(_ToolInput):
    goal: str
    strategy: str | None = None
    nodes: list[CreatePlanNodeInput]


class GetPlanInput(_ToolInput):
    plan_id: str | None = None
    view: PlanGetView = PlanGetView.SUMMARY
    node_id: str | None = None
    status_filter: list[PlanNodeStatus] = Field(default_factory=list)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=10, ge=1)

    @model_validator(mode="after")
    def validate_view(self) -> "GetPlanInput":
        if self.view == PlanGetView.NODE and not self.node_id:
            raise ValueError("node view requires node_id")
        return self


class AddNodesInput(_ToolInput):
    plan_id: str
    expected_revision: int = Field(ge=1)
    nodes: list[AddPlanNodeInput]


class UpdateNodeInput(_ToolInput):
    plan_id: str
    expected_revision: int = Field(ge=1)
    node_id: str
    title: str | None = None
    objective: str | None = None
    kind: PlanNodeKind | None = None
    success_criteria: list[str] | None = None
    depends_on: list[str] | None = None


class TransitionNodeInput(_ToolInput):
    plan_id: str
    expected_revision: int = Field(ge=1)
    node_id: str
    transition: PlanNodeTransition
    outcome_summary: str | None = None
    reason: str | None = None
    result_refs: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)


class RemoveNodeInput(_ToolInput):
    plan_id: str
    expected_revision: int = Field(ge=1)
    node_id: str


class CancelPlanInput(_ToolInput):
    plan_id: str
    expected_revision: int = Field(ge=1)
    reason: str


@dataclass(slots=True)
class PlanningToolOutcome:
    payload: dict[str, Any]
    event_type: str | None = None
    node_title: str | None = None
    severity: str = "info"
    visibility: str = "internal"


@dataclass(frozen=True, slots=True)
class PlanningToolDefinition:
    name: str
    description: str
    input_model: type[BaseModel]
    progress_key: str

    def parameters(self) -> dict[str, Any]:
        return self.input_model.model_json_schema()


PLANNING_TOOL_DEFINITIONS = (
    PlanningToolDefinition(
        name="agent_plan_create",
        description=(
            "Создать необязательный DAG-план текущего сложного цикла. "
            "Runtime создаёт plan/node ID и проверяет зависимости."
        ),
        input_model=CreatePlanInput,
        progress_key="agent_plan_create",
    ),
    PlanningToolDefinition(
        name="agent_plan_get",
        description=(
            "Точно получить summary, страницу узлов или один узел текущего "
            "DAG-плана. Это authoritative state, а не RAG-поиск."
        ),
        input_model=GetPlanInput,
        progress_key="agent_plan_get",
    ),
    PlanningToolDefinition(
        name="agent_plan_add_nodes",
        description=(
            "Добавить существенные узлы новой ревизией DAG-плана. "
            "Требует актуальный expected_revision."
        ),
        input_model=AddNodesInput,
        progress_key="agent_plan_add_nodes",
    ),
    PlanningToolDefinition(
        name="agent_plan_update_node",
        description=(
            "Изменить структуру только ещё не запускавшегося pending-узла. "
            "Lifecycle и runtime-owned поля этим инструментом не меняются."
        ),
        input_model=UpdateNodeInput,
        progress_key="agent_plan_update_node",
    ),
    PlanningToolDefinition(
        name="agent_plan_transition_node",
        description=(
            "Изменить lifecycle одного узла. Runtime проверяет зависимости, "
            "transition, revision и provenance result/artifact refs."
        ),
        input_model=TransitionNodeInput,
        progress_key="agent_plan_transition_node",
    ),
    PlanningToolDefinition(
        name="agent_plan_remove_node",
        description=(
            "Удалить только незапускавшийся pending-узел без dependants."
        ),
        input_model=RemoveNodeInput,
        progress_key="agent_plan_remove_node",
    ),
    PlanningToolDefinition(
        name="agent_plan_cancel",
        description=(
            "Явно отменить активный план новой terminal-ревизией с причиной."
        ),
        input_model=CancelPlanInput,
        progress_key="agent_plan_cancel",
    ),
)


class PlanningToolController:
    """Translate manager commands into PlanningService calls and safe payloads."""

    def __init__(self, service: PlanningService) -> None:
        self.service = service

    async def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        context: ManagerToolContext,
    ) -> PlanningToolOutcome:
        definition = next(
            (item for item in PLANNING_TOOL_DEFINITIONS if item.name == tool_name),
            None,
        )
        if definition is None:
            return PlanningToolOutcome(
                payload={
                    "type": "plan_validation_error",
                    "code": "unknown_plan_tool",
                    "message": "Unknown planning manager tool.",
                    "retryable": False,
                },
                event_type="plan_validation_failed",
                severity="error",
            )
        try:
            parsed = definition.input_model.model_validate(arguments)
            return await self._dispatch(tool_name, parsed, context)
        except ValidationError as error:
            return PlanningToolOutcome(
                payload={
                    "type": "plan_validation_error",
                    "code": "invalid_tool_arguments",
                    "message": "Planning tool arguments do not match the schema.",
                    "retryable": True,
                    "details": {"issue_count": error.error_count()},
                },
                event_type="plan_validation_failed",
                severity="warning",
            )
        except PlanRevisionConflictError as error:
            state = await self._refresh_after_conflict(error.plan_id, context)
            return PlanningToolOutcome(
                payload={
                    "type": "plan_revision_conflict",
                    "plan_id": error.plan_id,
                    "expected_revision": error.expected_revision,
                    "current_revision": error.current_revision,
                    "retryable": True,
                    "active_plan_state": (
                        state.model_dump(mode="json") if state else None
                    ),
                },
                event_type="plan_revision_conflict",
                severity="warning",
            )
        except PlanValidationError as error:
            return PlanningToolOutcome(
                payload={
                    "type": "plan_validation_error",
                    "code": error.code,
                    "message": error.safe_message,
                    "retryable": error.retryable,
                    "details": error.details,
                },
                event_type="plan_validation_failed",
                severity="warning",
            )
        except PlanAccessError:
            return PlanningToolOutcome(
                payload={
                    "type": "plan_access_error",
                    "message": "Plan is not accessible from the current cycle.",
                    "retryable": False,
                },
                event_type="plan_validation_failed",
                severity="error",
            )
        except PlanNotFoundError:
            return PlanningToolOutcome(
                payload={
                    "type": "plan_not_found",
                    "message": "Plan was not found.",
                    "retryable": False,
                },
                event_type="plan_validation_failed",
                severity="warning",
            )
        except PlanStorageError:
            raise

    async def _dispatch(
        self,
        tool_name: str,
        parsed: BaseModel,
        context: ManagerToolContext,
    ) -> PlanningToolOutcome:
        if tool_name == "agent_plan_create":
            return await self._create(parsed, context)
        if tool_name == "agent_plan_get":
            return await self._get(parsed, context)
        if tool_name == "agent_plan_add_nodes":
            return await self._add_nodes(parsed, context)
        if tool_name == "agent_plan_update_node":
            return await self._update_node(parsed, context)
        if tool_name == "agent_plan_transition_node":
            return await self._transition_node(parsed, context)
        if tool_name == "agent_plan_remove_node":
            return await self._remove_node(parsed, context)
        if tool_name == "agent_plan_cancel":
            return await self._cancel(parsed, context)
        raise PlanValidationError(
            "unknown_plan_tool",
            "Unknown planning manager tool.",
            retryable=False,
        )

    async def _create(
        self,
        parsed: CreatePlanInput,
        context: ManagerToolContext,
    ) -> PlanningToolOutcome:
        plan, node_map = await self.service.create_plan(
            session_id=context.session_id,
            cycle_id=context.cycle_id,
            goal=parsed.goal,
            strategy=parsed.strategy,
            nodes=parsed.nodes,
        )
        state = self.service._state(plan)
        self._sync_cycle(context, state)
        return PlanningToolOutcome(
            payload={
                "type": "plan_created",
                "plan_id": plan.plan_id,
                "revision": plan.revision,
                "node_id_map": node_map,
                "active_plan_state": state.model_dump(mode="json"),
            },
            event_type="plan_created",
            severity="success",
            visibility="user",
        )

    async def _get(
        self,
        parsed: GetPlanInput,
        context: ManagerToolContext,
    ) -> PlanningToolOutcome:
        plan_id = self._resolve_plan_id(parsed.plan_id, context)
        plan = await self.service.get_plan(
            session_id=context.session_id,
            cycle_id=context.cycle_id,
            plan_id=plan_id,
        )
        if parsed.view == PlanGetView.SUMMARY:
            state = self.service._state(plan)
            self._sync_cycle(context, state)
            payload = state.model_dump(mode="json")
        elif parsed.view == PlanGetView.NODE:
            node = next(
                (item for item in plan.nodes if item.node_id == parsed.node_id),
                None,
            )
            if node is None:
                raise PlanValidationError(
                    "node_not_found",
                    "Plan node was not found.",
                    details={"node_id": parsed.node_id},
                )
            payload = {
                "type": "plan_node",
                "plan_id": plan.plan_id,
                "revision": plan.revision,
                "node": node.model_dump(mode="json"),
            }
        else:
            limit = min(parsed.limit, self.service.config.max_plan_get_limit)
            items = [
                item
                for item in plan.nodes
                if not parsed.status_filter or item.status in parsed.status_filter
            ]
            page = items[parsed.offset:parsed.offset + limit]
            payload = {
                "type": "plan_nodes",
                "plan_id": plan.plan_id,
                "revision": plan.revision,
                "offset": parsed.offset,
                "limit": limit,
                "total": len(items),
                "items": [item.model_dump(mode="json") for item in page],
            }
        return PlanningToolOutcome(payload=payload)

    async def _add_nodes(
        self,
        parsed: AddNodesInput,
        context: ManagerToolContext,
    ) -> PlanningToolOutcome:
        plan, node_map = await self.service.add_nodes(
            session_id=context.session_id,
            cycle_id=context.cycle_id,
            plan_id=parsed.plan_id,
            expected_revision=parsed.expected_revision,
            nodes=parsed.nodes,
        )
        state = self.service._state(plan)
        self._sync_cycle(context, state)
        return PlanningToolOutcome(
            payload={
                "type": "plan_nodes_added",
                "plan_id": plan.plan_id,
                "previous_revision": parsed.expected_revision,
                "revision": plan.revision,
                "node_id_map": node_map,
                "active_plan_state": state.model_dump(mode="json"),
            },
            event_type="plan_revised",
        )

    async def _update_node(
        self,
        parsed: UpdateNodeInput,
        context: ManagerToolContext,
    ) -> PlanningToolOutcome:
        plan = await self.service.update_node(
            session_id=context.session_id,
            cycle_id=context.cycle_id,
            plan_id=parsed.plan_id,
            expected_revision=parsed.expected_revision,
            node_id=parsed.node_id,
            title=parsed.title,
            objective=parsed.objective,
            kind=parsed.kind,
            success_criteria=parsed.success_criteria,
            depends_on=parsed.depends_on,
        )
        state = self.service._state(plan)
        self._sync_cycle(context, state)
        return PlanningToolOutcome(
            payload={
                "type": "plan_node_updated",
                "plan_id": plan.plan_id,
                "node_id": parsed.node_id,
                "previous_revision": parsed.expected_revision,
                "revision": plan.revision,
                "active_plan_state": state.model_dump(mode="json"),
            },
            event_type="plan_revised",
        )

    async def _transition_node(
        self,
        parsed: TransitionNodeInput,
        context: ManagerToolContext,
    ) -> PlanningToolOutcome:
        before = await self.service.get_plan(
            session_id=context.session_id,
            cycle_id=context.cycle_id,
            plan_id=parsed.plan_id,
        )
        node = next(
            (item for item in before.nodes if item.node_id == parsed.node_id),
            None,
        )
        if node is None:
            raise PlanValidationError(
                "node_not_found",
                "Plan node was not found.",
                details={"node_id": parsed.node_id},
            )
        plan = await self.service.transition_node(
            session_id=context.session_id,
            cycle_id=context.cycle_id,
            plan_id=parsed.plan_id,
            expected_revision=parsed.expected_revision,
            node_id=parsed.node_id,
            transition=parsed.transition,
            outcome_summary=parsed.outcome_summary,
            reason=parsed.reason,
            result_refs=parsed.result_refs,
            artifact_refs=parsed.artifact_refs,
            runtime_result_refs=context.active_cycle.result_refs,
            runtime_artifact_refs=context.active_cycle.artifact_refs,
        )
        state = self.service._state(plan)
        self._sync_cycle(context, state)
        event_by_transition = {
            PlanNodeTransition.START: "plan_node_started",
            PlanNodeTransition.COMPLETE: "plan_node_completed",
            PlanNodeTransition.BLOCK: "plan_node_blocked",
            PlanNodeTransition.FAIL: "plan_node_failed",
            PlanNodeTransition.SKIP: "plan_node_skipped",
            PlanNodeTransition.RETRY: "plan_revised",
        }
        event_type = (
            "plan_completed"
            if plan.status == PlanStatus.COMPLETED
            else event_by_transition[parsed.transition]
        )
        return PlanningToolOutcome(
            payload={
                "type": "plan_node_transitioned",
                "plan_id": plan.plan_id,
                "node_id": parsed.node_id,
                "transition": parsed.transition.value,
                "previous_revision": parsed.expected_revision,
                "revision": plan.revision,
                "plan_completed": plan.status == PlanStatus.COMPLETED,
                "active_plan_state": state.model_dump(mode="json"),
            },
            event_type=event_type,
            node_title=node.title,
            severity=(
                "error"
                if parsed.transition == PlanNodeTransition.FAIL
                else "success"
                if parsed.transition in {
                    PlanNodeTransition.START,
                    PlanNodeTransition.COMPLETE,
                }
                else "warning"
            ),
            visibility="user",
        )

    async def _remove_node(
        self,
        parsed: RemoveNodeInput,
        context: ManagerToolContext,
    ) -> PlanningToolOutcome:
        plan = await self.service.remove_node(
            session_id=context.session_id,
            cycle_id=context.cycle_id,
            plan_id=parsed.plan_id,
            expected_revision=parsed.expected_revision,
            node_id=parsed.node_id,
        )
        state = self.service._state(plan)
        self._sync_cycle(context, state)
        return PlanningToolOutcome(
            payload={
                "type": "plan_node_removed",
                "plan_id": plan.plan_id,
                "node_id": parsed.node_id,
                "previous_revision": parsed.expected_revision,
                "revision": plan.revision,
                "active_plan_state": state.model_dump(mode="json"),
            },
            event_type="plan_revised",
        )

    async def _cancel(
        self,
        parsed: CancelPlanInput,
        context: ManagerToolContext,
    ) -> PlanningToolOutcome:
        plan = await self.service.cancel_plan(
            session_id=context.session_id,
            cycle_id=context.cycle_id,
            plan_id=parsed.plan_id,
            expected_revision=parsed.expected_revision,
            reason=parsed.reason,
        )
        state = self.service._state(plan)
        self._sync_cycle(context, state)
        return PlanningToolOutcome(
            payload={
                "type": "plan_cancelled",
                "plan_id": plan.plan_id,
                "previous_revision": parsed.expected_revision,
                "revision": plan.revision,
                "reason": parsed.reason.strip(),
                "active_plan_state": state.model_dump(mode="json"),
            },
            event_type="plan_cancelled",
            severity="warning",
            visibility="user",
        )

    async def _refresh_after_conflict(
        self,
        plan_id: str,
        context: ManagerToolContext,
    ) -> ActivePlanState | None:
        try:
            state = await self.service.get_active_state(
                session_id=context.session_id,
                cycle_id=context.cycle_id,
                plan_id=plan_id,
            )
        except Exception:
            return None
        self._sync_cycle(context, state)
        return state

    @staticmethod
    def _resolve_plan_id(
        plan_id: str | None,
        context: ManagerToolContext,
    ) -> str:
        resolved = plan_id or context.active_cycle.active_plan_id
        if not resolved:
            raise PlanValidationError(
                "active_plan_not_found",
                "Current cycle does not have an active plan.",
            )
        return resolved

    @staticmethod
    def _sync_cycle(
        context: ManagerToolContext,
        state: ActivePlanState,
    ) -> None:
        cycle = context.active_cycle
        cycle.active_plan_id = state.plan_id
        cycle.active_plan_revision = state.revision
        cycle.active_plan_node_id = (
            state.current_node.node_id if state.current_node else None
        )
        cycle.active_plan_state = state
        cycle.plan_reconciliation_attempts = 0
