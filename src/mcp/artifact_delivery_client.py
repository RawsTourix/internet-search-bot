"""Delivery-aware layer above artifact core and below optional DAG planning."""

from __future__ import annotations

from typing import Any

from ..artifacts.delivery_tools import (
    ARTIFACT_DELIVERY_TOOL_DEFINITIONS,
    ARTIFACT_DELIVERY_TOOL_NAMES,
    ArtifactDeliveryToolController,
)
from ..artifacts.runtime import ArtifactRuntimeCoordinator
from .artifact_client import ArtifactMCPClient, _ARTIFACT_MUTATION_TOOL_NAMES
from .artifact_request_context import (
    reset_artifact_request_client_type,
    set_artifact_request_client_type,
)
from .manager_runtime_context import (
    get_manager_context,
    reset_manager_context,
    set_manager_context,
)
from .mcp_client import MCPClient, ManagerToolSpec
from .schema import inline_local_schema_refs


class ArtifactDeliveryMCPClient(ArtifactMCPClient):
    """Add exact delivery selection without moving transport IO into agent loop."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        artifacts_enabled = (
            self.artifact_services is not None
            and self.artifact_config is not None
            and self.artifact_config.enabled
        )
        self.artifact_delivery_tool_controller = (
            ArtifactDeliveryToolController(
                self.artifact_services.delivery_service
            )
            if artifacts_enabled
            else None
        )
        if artifacts_enabled:
            self.artifact_runtime = ArtifactRuntimeCoordinator(
                self.artifact_services.artifact_service,
                self.artifact_services.delivery_service,
            )

    def _build_manager_tools(self) -> dict[str, ManagerToolSpec]:
        tools = super()._build_manager_tools()
        if self.artifact_delivery_tool_controller is None:
            return tools

        for definition in ARTIFACT_DELIVERY_TOOL_DEFINITIONS:
            async def handler(
                arguments: dict[str, Any],
                *,
                tool_name: str = definition.name,
            ) -> dict[str, Any]:
                context = get_manager_context()
                if context is None:
                    return {
                        "type": "artifact_context_error",
                        "message": "Artifact tool requires an active agent cycle.",
                        "retryable": False,
                    }
                outcome = await self.artifact_delivery_tool_controller.execute(
                    tool_name,
                    arguments,
                    context,
                )
                await self._record_artifact_outcome(outcome, context)
                return outcome.payload

            tools[definition.name] = ManagerToolSpec(
                name=definition.name,
                description=definition.description,
                parameters=inline_local_schema_refs(definition.parameters()),
                handler=handler,
                progress_key=definition.progress_key,
            )
        return tools

    async def process_query(self, *args: Any, **kwargs: Any):
        manager_token = set_manager_context(None)
        client_token = set_artifact_request_client_type(
            kwargs.get("client_type")
        )
        try:
            # Call the shared base loop directly. Artifact/planning behavior remains
            # active through virtual hooks, while the manager context stays available
            # long enough to build the final delivery projection.
            result = await MCPClient.process_query(self, *args, **kwargs)
            context = get_manager_context()
            if (
                context is not None
                and self.artifact_services is not None
                and self.artifact_config is not None
                and self.artifact_config.enabled
            ):
                refs = await self.artifact_services.delivery_service.list_cycle_refs(
                    session_id=context.session_id,
                    cycle_id=context.cycle_id,
                )
                result.artifacts = [
                    item.model_dump(mode="json") for item in refs
                ]
            return result
        finally:
            reset_artifact_request_client_type(client_token)
            reset_manager_context(manager_token)

    async def _call_registered_tool(
        self,
        public_tool_name: str,
        arguments: dict[str, Any],
    ):
        if public_tool_name in ARTIFACT_DELIVERY_TOOL_NAMES:
            context = get_manager_context()
            if context is None or self.artifact_delivery_tool_controller is None:
                payload = {
                    "type": "artifact_context_error",
                    "message": "Artifact tool requires an active agent cycle.",
                    "retryable": False,
                }
            elif (
                public_tool_name in _ARTIFACT_MUTATION_TOOL_NAMES
                and self._active_plan_requires_node(context)
            ):
                state = context.active_cycle.active_plan_state
                payload = {
                    "type": "plan_node_required",
                    "plan_id": getattr(state, "plan_id", None),
                    "revision": getattr(state, "revision", None),
                    "message": (
                        "Before selecting an artifact for delivery, start one "
                        "ready plan node."
                    ),
                    "retryable": True,
                }
                self._trace_event(
                    context.active_cycle.cycle_trace,
                    "plan_tool_call_blocked",
                    plan_id=payload["plan_id"],
                    revision=payload["revision"],
                    blocked_tool=public_tool_name,
                )
            else:
                outcome = await self.artifact_delivery_tool_controller.execute(
                    public_tool_name,
                    arguments,
                    context,
                )
                await self._record_artifact_outcome(outcome, context)
                payload = outcome.payload
                if payload.get("type") in {
                    "artifact_delivery_selected",
                    "artifact_delivery_cancelled",
                }:
                    await self._refresh_artifact_state(context)
            return self._text_result(payload)

        return await super()._call_registered_tool(public_tool_name, arguments)
