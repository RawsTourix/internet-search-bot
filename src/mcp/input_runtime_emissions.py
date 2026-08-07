"""IR-6 manager-tool bridge for durable semantic intermediate messages."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from mcp.types import TextContent

from ..agent.protocol import dumps_json
from ..input_runtime.composition import get_input_runtime_binding
from ..input_runtime.emissions import ManagerToolExecutionContext
from .input_runtime_checkpoints import _checkpoint_active_cycle
from .mcp_client import ManagerToolSpec


class InputRuntimeEmissionMixin:
    """Bind semantic manager calls to exact runtime-owned cycle authority.

    No session/cycle/route value comes from LLM arguments. The active-cycle
    ContextVar is scoped/reset by InputRuntimeCheckpointMixin and is safe across
    concurrent sessions; the native tool_call_id is read from that exact cycle's
    runtime trace immediately after the base loop recorded the call.
    """

    EMISSION_MANAGER_TOOL = "send_user_message"

    def _build_manager_tools(self):
        tools = super()._build_manager_tools()
        tools[self.EMISSION_MANAGER_TOOL] = ManagerToolSpec(
            name=self.EMISSION_MANAGER_TOOL,
            description=(
                "Отправить пользователю отдельное содержательное промежуточное "
                "сообщение, не завершая задачу. Используй только для значимого "
                "partial result, существенного риска/противоречия, важного нового "
                "этапа долгой работы, заметной смены подхода или когда пользователь "
                "явно просил содержательные updates. Не используй на каждой "
                "итерации, перед каждым tool call, для обычного 'работаю', debug "
                "log, вместо финального ответа или вместо ask_user."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "Содержательное промежуточное сообщение пользователю."
                        ),
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["intermediate"],
                        "default": "intermediate",
                    },
                    "importance": {
                        "type": "string",
                        "enum": ["normal", "high"],
                        "default": "normal",
                    },
                },
                "required": ["message"],
                "additionalProperties": False,
            },
            handler=self._manager_send_user_message_unscoped,
            progress_key="tool_start",
        )
        return tools

    async def _manager_send_user_message_unscoped(
        self,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        # Direct manager invocation has no native tool-call identity and must not
        # manufacture authority. Production calls are intercepted below.
        return {
            "type": "agent_emission_result",
            "accepted": False,
            "reason_code": "runtime_context_unavailable",
            "delivery_required_for_cycle": False,
        }

    def _tool_start_message(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        progress_locale: str = "ru",
    ) -> str:
        if tool_name == self.EMISSION_MANAGER_TOOL:
            return self._progress_text(
                "tool_start",
                locale_name=progress_locale,
                tool_name=tool_name,
            )
        return super()._tool_start_message(
            tool_name,
            arguments,
            progress_locale=progress_locale,
        )

    @staticmethod
    def _native_tool_call_id(active_cycle: Any) -> str | None:
        for event in reversed(list(getattr(active_cycle, "cycle_trace", ()) or ())):
            if (
                event.get("type") == "tool_call"
                and event.get("tool_name") == InputRuntimeEmissionMixin.EMISSION_MANAGER_TOOL
            ):
                value = str(event.get("tool_call_id") or "").strip()
                return value or None
        return None

    def _manager_tool_execution_context(self) -> ManagerToolExecutionContext | None:
        active_cycle = _checkpoint_active_cycle.get()
        if active_cycle is None:
            return None
        tool_call_id = self._native_tool_call_id(active_cycle)
        revision_id = str(
            getattr(active_cycle, "active_context_revision_id", "") or ""
        ).strip()
        session_id = str(getattr(active_cycle, "session_id", "") or "").strip()
        cycle_id = str(getattr(active_cycle, "cycle_id", "") or "").strip()
        original_input_batch_id = str(
            getattr(active_cycle, "original_input_batch_id", "") or ""
        ).strip()
        generation = getattr(active_cycle, "input_runtime_generation", None)
        if (
            not tool_call_id
            or not revision_id
            or not session_id
            or not cycle_id
            or not original_input_batch_id
            or isinstance(generation, bool)
            or not isinstance(generation, int)
            or generation < 0
        ):
            return None
        return ManagerToolExecutionContext(
            session_id=session_id,
            cycle_id=cycle_id,
            generation=generation,
            context_revision_id=revision_id,
            tool_call_id=tool_call_id,
            original_input_batch_id=original_input_batch_id,
        )

    async def _manager_send_user_message(
        self,
        arguments: dict[str, Any],
        execution_context: ManagerToolExecutionContext,
    ) -> dict[str, Any]:
        binding = get_input_runtime_binding()
        if binding is None or not binding.config.enabled:
            return {
                "type": "agent_emission_result",
                "accepted": False,
                "reason_code": "input_runtime_unavailable",
                "delivery_required_for_cycle": False,
            }
        return await binding.emission_service.emit_intermediate(
            context=execution_context,
            message=arguments.get("message"),
            kind=arguments.get("kind", "intermediate"),
            importance=arguments.get("importance", "normal"),
        )

    async def _call_registered_tool(
        self,
        public_tool_name: str,
        arguments: dict[str, Any],
    ):
        if public_tool_name != self.EMISSION_MANAGER_TOOL:
            return await super()._call_registered_tool(public_tool_name, arguments)
        execution_context = self._manager_tool_execution_context()
        if execution_context is None:
            data = await self._manager_send_user_message_unscoped(arguments)
        else:
            data = await self._manager_send_user_message(
                arguments,
                execution_context,
            )
        return SimpleNamespace(
            content=[TextContent(type="text", text=dumps_json(data))]
        )

    def _tool_result_payload(
        self,
        tool_name: str,
        tool_result: str,
    ) -> dict[str, Any]:
        if tool_name == self.EMISSION_MANAGER_TOOL:
            try:
                parsed = json.loads(tool_result)
            except Exception:
                parsed = None
            if isinstance(parsed, dict) and parsed.get("type") == "agent_emission_result":
                # Runtime generated evidence, not arbitrary external tool output.
                parsed["trusted"] = True
                parsed["runtime_generated"] = True
                return parsed
        return super()._tool_result_payload(tool_name, tool_result)
