"""Production composition for scoped artifact catalog activation."""

from __future__ import annotations

from typing import Any

from ..artifacts.scoped_tools import (
    SCOPED_ARTIFACT_NATIVE_TOOL_DEFINITIONS,
    ScopedArtifactToolController,
)
from .manager_runtime_context import get_manager_context
from .mcp_client import ManagerToolSpec
from .schema import inline_local_schema_refs


_SCOPED_LIST_DEFINITION = next(
    item
    for item in SCOPED_ARTIFACT_NATIVE_TOOL_DEFINITIONS
    if item.name == "artifact_list"
)


class ArtifactAccessScopeMixin:
    """Add current/session/workspace catalog scopes to the production client."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if (
            self.artifact_services is not None
            and self.artifact_config is not None
            and self.artifact_config.enabled
        ):
            self.artifact_tool_controller = ScopedArtifactToolController(
                self.artifact_services.artifact_service,
                self.artifact_services.delivery_service,
            )

    def _build_manager_tools(self) -> dict[str, ManagerToolSpec]:
        tools = super()._build_manager_tools()
        if (
            self.artifact_services is None
            or self.artifact_config is None
            or not self.artifact_config.enabled
        ):
            return tools

        async def artifact_list_handler(arguments: dict[str, Any]) -> dict[str, Any]:
            context = get_manager_context()
            if context is None or self.artifact_tool_controller is None:
                return {
                    "type": "artifact_context_error",
                    "message": "Artifact tool requires an active agent cycle.",
                    "retryable": False,
                }
            outcome = await self.artifact_tool_controller.execute(
                "artifact_list",
                arguments,
                context,
            )
            await self._record_artifact_outcome(outcome, context)
            await self._refresh_artifact_state(context)
            return outcome.payload

        tools["artifact_list"] = ManagerToolSpec(
            name="artifact_list",
            description=_SCOPED_LIST_DEFINITION.description,
            parameters=inline_local_schema_refs(
                _SCOPED_LIST_DEFINITION.parameters()
            ),
            handler=artifact_list_handler,
            progress_key=_SCOPED_LIST_DEFINITION.progress_key,
        )
        return tools

    async def _call_registered_tool(
        self,
        public_tool_name: str,
        arguments: dict[str, Any],
    ):
        result = await super()._call_registered_tool(public_tool_name, arguments)
        if public_tool_name == "artifact_list":
            context = get_manager_context()
            if context is not None:
                await self._refresh_artifact_state(context)
        return result
