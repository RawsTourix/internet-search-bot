"""Delivery-aware mixin above artifact core and optional DAG planning."""

from __future__ import annotations

from typing import Any

from ..artifacts.delivery_tools import (
    ARTIFACT_DELIVERY_TOOL_DEFINITIONS,
    ARTIFACT_DELIVERY_TOOL_NAMES,
    ArtifactDeliveryToolController,
)
from ..artifacts.runtime import ArtifactRuntimeCoordinator
from .artifact_client import ArtifactMCPClient
from .artifact_request_context import (
    get_artifact_request_client_type,
    get_artifact_request_cycle_identity,
    reset_artifact_request_client_type,
    reset_artifact_request_cycle_identity,
    set_artifact_request_client_type,
    set_artifact_request_cycle_identity,
)
from .manager_context import ManagerToolContext
from .manager_runtime_context import get_manager_context
from .mcp_client import ManagerToolSpec, SessionState
from .schema import inline_local_schema_refs


class ArtifactDeliveryMixin:
    """Add delivery control without bypassing the inherited agent runtime."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        artifact_services = kwargs.get("artifact_services")
        artifacts_enabled = (
            artifact_services is not None
            and artifact_services.config.enabled
        )
        self.artifact_delivery_tool_controller = (
            ArtifactDeliveryToolController(artifact_services.delivery_service)
            if artifacts_enabled
            else None
        )
        super().__init__(*args, **kwargs)
        if artifacts_enabled:
            self.artifact_runtime = ArtifactRuntimeCoordinator(
                artifact_services.artifact_service,
                artifact_services.delivery_service,
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
                await self._record_delivery_outcome(outcome, context)
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
        client_token = set_artifact_request_client_type(
            kwargs.get("client_type")
        )
        identity_token = set_artifact_request_cycle_identity(None)
        try:
            result = await super().process_query(*args, **kwargs)
            identity = get_artifact_request_cycle_identity()
            if (
                identity is not None
                and self.artifact_services is not None
                and self.artifact_config is not None
                and self.artifact_config.enabled
            ):
                session_id, cycle_id = identity
                refs = await self.artifact_services.delivery_service.list_cycle_refs(
                    session_id=session_id,
                    cycle_id=cycle_id,
                )
                result.artifacts = [
                    item.model_dump(mode="json") for item in refs
                ]
            return result
        finally:
            reset_artifact_request_cycle_identity(identity_token)
            reset_artifact_request_client_type(client_token)

    def _activate_manager_context(
        self,
        *,
        active_cycle,
        state: SessionState,
        session_id: str,
        progress_callback,
    ) -> ManagerToolContext:
        context = super()._activate_manager_context(
            active_cycle=active_cycle,
            state=state,
            session_id=session_id,
            progress_callback=progress_callback,
        )
        context.client_type = get_artifact_request_client_type()
        set_artifact_request_cycle_identity(
            (context.session_id, context.cycle_id)
        )
        return context

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
            elif self._active_plan_requires_node(context):
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
                await self._record_delivery_outcome(outcome, context)
                payload = outcome.payload
                if payload.get("type") in {
                    "artifact_delivery_selected",
                    "artifact_delivery_cancelled",
                }:
                    await self._refresh_artifact_state(context)
            return self._text_result(payload)

        return await super()._call_registered_tool(public_tool_name, arguments)

    async def _record_delivery_outcome(
        self,
        outcome,
        context: ManagerToolContext,
    ) -> None:
        if outcome.event_type is None:
            return
        delivery = outcome.payload.get("delivery")
        safe_data: dict[str, Any] = {}
        if isinstance(delivery, dict):
            for key in (
                "delivery_id",
                "artifact_id",
                "filename",
                "format_id",
                "size_bytes",
                "client_type",
                "state",
            ):
                if delivery.get(key) is not None:
                    safe_data[key] = delivery[key]
        self._trace_event(
            context.active_cycle.cycle_trace,
            outcome.event_type,
            **safe_data,
        )
        await self._emit_progress_event(
            state=context.session_state,
            session_id=context.session_id,
            cycle_id=context.cycle_id,
            progress_callback=context.progress_callback,
            cycle_trace=context.active_cycle.cycle_trace,
            event_type=outcome.event_type,
            severity=outcome.severity,
            visibility=outcome.visibility,
            data=safe_data,
            message_kwargs={"filename": str(safe_data.get("filename") or "")},
        )


class ArtifactDeliveryMCPClient(ArtifactDeliveryMixin, ArtifactMCPClient):
    """Artifact client with durable delivery selection and result projection."""
