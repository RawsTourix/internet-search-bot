"""Delivery-aware mixin above artifact core and optional DAG planning."""

from __future__ import annotations

from typing import Any

from ..agent.protocol import dumps_json
from ..artifacts.delivery_tools import (
    ARTIFACT_DELIVERY_TOOL_DEFINITIONS,
    ARTIFACT_DELIVERY_TOOL_NAMES,
    ArtifactDeliveryToolController,
)
from ..artifacts.errors import ArtifactAccessError
from ..artifacts.runtime import ArtifactRuntimeCoordinator
from ..artifacts.tools import (
    ArtifactResultPolicy,
    ToolExecutionDisposition,
)
from .artifact_client import ArtifactMCPClient
from .artifact_request_context import (
    get_artifact_request_client_type,
    get_artifact_request_cycle_identity,
    get_artifact_request_input_batch,
    reset_artifact_request_client_type,
    reset_artifact_request_cycle_identity,
    reset_artifact_request_input_batch,
    set_artifact_request_client_type,
    set_artifact_request_cycle_identity,
    set_artifact_request_input_batch,
)
from .manager_context import ManagerToolContext
from .manager_runtime_context import get_manager_context
from .mcp_client import ManagerToolSpec, SessionState
from .schema import inline_local_schema_refs


class ArtifactDeliveryMixin:
    """Add input artifacts and delivery without bypassing inherited runtime."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.defer_cycle_done_for_output = bool(
            kwargs.pop("defer_cycle_done_for_output", False)
        )
        artifact_services = kwargs.get("artifact_services")
        artifacts_enabled = (
            artifact_services is not None
            and artifact_services.config.enabled
        )
        self._session_artifact_handoffs: dict[str, list[str]] = {}
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
        input_batch = kwargs.pop("input_batch", None)
        client_type = kwargs.get("client_type")
        if client_type is None and input_batch is not None:
            client_type = input_batch.client_type
            kwargs["client_type"] = client_type

        if input_batch is not None:
            query = dumps_json(input_batch.to_agent_payload())
            if args:
                args = (query, *args[1:])
            else:
                kwargs["query"] = query

        client_token = set_artifact_request_client_type(client_type)
        identity_token = set_artifact_request_cycle_identity(None)
        batch_token = set_artifact_request_input_batch(input_batch)
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
                result.cycle_id = cycle_id
                refs = await self.artifact_services.delivery_service.list_cycle_refs(
                    session_id=session_id,
                    cycle_id=cycle_id,
                )
                result.artifacts = [
                    item.model_dump(mode="json") for item in refs
                ]
            return result
        finally:
            reset_artifact_request_input_batch(batch_token)
            reset_artifact_request_cycle_identity(identity_token)
            reset_artifact_request_client_type(client_token)

    def _append_dialog_turn(self, session, **kwargs: Any) -> None:
        """Persist a bounded exact artifact handoff for the next session turn."""

        super()._append_dialog_turn(session, **kwargs)
        context = get_manager_context()
        if context is None:
            return

        maximum = (
            self.artifact_config.max_artifacts_per_cycle
            if self.artifact_config is not None
            else 32
        )
        refs = list(dict.fromkeys(context.active_cycle.artifact_refs))[-maximum:]
        handoffs = getattr(self, "_session_artifact_handoffs", None)
        if handoffs is None:
            handoffs = {}
            self._session_artifact_handoffs = handoffs
        if refs:
            handoffs[context.session_id] = refs
        else:
            handoffs.pop(context.session_id, None)

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

        inherited_refs = list(
            getattr(self, "_session_artifact_handoffs", {}).get(
                context.session_id,
                (),
            )
        )
        added_refs: list[str] = []
        for artifact_id in inherited_refs:
            if artifact_id not in context.active_cycle.artifact_refs:
                context.active_cycle.artifact_refs.append(artifact_id)
                added_refs.append(artifact_id)
        if added_refs:
            self._trace_event(
                context.active_cycle.cycle_trace,
                "artifact_authority_inherited",
                artifact_count=len(added_refs),
                artifact_ids=added_refs,
            )

        input_batch = get_artifact_request_input_batch()
        if input_batch is not None:
            if input_batch.session_id != context.session_id:
                raise ArtifactAccessError(
                    "Committed input batch belongs to another session"
                )
            existing_batch_id = context.active_cycle.original_input_batch_id
            if (
                existing_batch_id is not None
                and existing_batch_id != input_batch.input_batch_id
            ):
                raise ArtifactAccessError(
                    "Additional committed batches require CycleInbox runtime"
                )
            context.active_cycle.original_input_batch_id = input_batch.input_batch_id
            for artifact_id in input_batch.artifact_refs:
                if artifact_id not in context.active_cycle.artifact_refs:
                    context.active_cycle.artifact_refs.append(artifact_id)
        return context

    async def _refresh_artifact_state(
        self,
        context: ManagerToolContext,
    ) -> None:
        if (
            self.artifact_services is not None
            and self.artifact_config is not None
            and self.artifact_config.enabled
        ):
            available = await self.artifact_services.candidate_store.list_cycle(
                session_id=context.session_id,
                cycle_id=context.cycle_id,
            )
            context.active_cycle.artifact_candidate_refs = [
                item.candidate_id for item in available
            ]
        await super()._refresh_artifact_state(context)

    async def _call_registered_tool(
        self,
        public_tool_name: str,
        arguments: dict[str, Any],
    ):
        if public_tool_name in ARTIFACT_DELIVERY_TOOL_NAMES:
            outcome = None
            context = get_manager_context()
            if context is None or self.artifact_delivery_tool_controller is None:
                payload = {
                    "type": "artifact_context_error",
                    "message": "Artifact tool requires an active agent cycle.",
                    "retryable": False,
                }
                disposition = ToolExecutionDisposition.REJECTED
                result_policy = ArtifactResultPolicy.INLINE_RECEIPT
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
                disposition = ToolExecutionDisposition.REJECTED
                result_policy = ArtifactResultPolicy.INLINE_RECEIPT
            else:
                outcome = await self.artifact_delivery_tool_controller.execute(
                    public_tool_name,
                    arguments,
                    context,
                )
                await self._record_delivery_outcome(outcome, context)
                payload = outcome.payload
                disposition = outcome.disposition
                result_policy = outcome.result_policy
                if payload.get("type") in {
                    "artifact_delivery_batch_selected",
                    "artifact_delivery_batch_cancelled",
                }:
                    await self._refresh_artifact_state(context)
            return self._text_result(
                payload,
                disposition=disposition,
                result_policy=result_policy,
            )
        return await super()._call_registered_tool(public_tool_name, arguments)

    async def _emit_progress_event(self, *args: Any, **kwargs: Any) -> None:
        """Do not announce final success before the OutputBatch is delivered."""
        if (
            kwargs.get("event_type") == "cycle_done"
            and self.defer_cycle_done_for_output
            and get_artifact_request_input_batch() is not None
        ):
            kwargs["event_type"] = "result_ready"
            kwargs["message_key"] = "result_ready"
        await super()._emit_progress_event(*args, **kwargs)

    def _build_final_evidence_pack(self, **kwargs: Any) -> dict[str, Any]:
        evidence = super()._build_final_evidence_pack(**kwargs)
        context = get_manager_context()
        if context is None or context.active_cycle.artifact_state is None:
            return evidence

        state = context.active_cycle.artifact_state
        evidence["artifact_state"] = state.model_dump(mode="json")
        if state.deliveries:
            evidence["artifact_delivery_state_contract"] = {
                "selected": (
                    "The exact artifact was selected for client delivery, but "
                    "transport delivery has not happened yet."
                ),
                "delivering": (
                    "The client transport claimed the delivery, but completion "
                    "has not been confirmed yet."
                ),
                "delivered": (
                    "The client transport confirmed successful delivery."
                ),
                "failed": "The client transport confirmed delivery failure.",
                "unknown": (
                    "Transport outcome is ambiguous and must not be described as "
                    "successfully delivered."
                ),
                "rule": (
                    "Never say that a file was sent or delivered when the latest "
                    "known state is only selected or delivering."
                ),
            }
        return evidence

    async def _record_delivery_outcome(
        self,
        outcome,
        context: ManagerToolContext,
    ) -> None:
        if outcome.event_type is None:
            return
        safe_data: dict[str, Any] = {}
        deliveries = outcome.payload.get("items") or []
        for delivery in deliveries:
            if not isinstance(delivery, dict):
                continue
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
                    safe_data.setdefault(f"{key}s", []).append(delivery[key])
        if deliveries:
            safe_data["requested_count"] = outcome.payload.get(
                "requested_count",
                len(deliveries),
            )
            safe_data["selected_count"] = outcome.payload.get(
                "selected_count",
                0,
            )
            safe_data["cancelled_count"] = outcome.payload.get(
                "cancelled_count",
                0,
            )
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
            message_kwargs={
                "filename": str(
                    (safe_data.get("filenames") or [""])[0]
                )
            },
        )


class ArtifactDeliveryMCPClient(ArtifactDeliveryMixin, ArtifactMCPClient):
    """Artifact client with durable delivery selection and result projection."""
