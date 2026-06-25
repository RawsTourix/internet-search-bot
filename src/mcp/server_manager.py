from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .mcp_client import MCPClient


class MCPServerManager:
    """
    Runtime-менеджер MCP-серверов.

    v0.1:
    - работает поверх текущего MCPClient;
    - не хранит собственные подключения;
    - использует server_runtimes/tool_registry/available_tools владельца.
    """

    def __init__(self, owner: "MCPClient"):
        self.owner = owner

    def list_servers(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []

        # Если connect_to_servers ещё не вызывался,
        # server_configs_by_name может быть пустым.
        for name, config in self.owner.server_configs_by_name.items():
            runtime = self.owner.server_runtimes.get(name)

            result.append({
                "name": name,
                "alias": config.alias or "",
                "enabled_in_config": bool(config.enabled),
                "connected": runtime is not None,
                "connect_type": config.connect_type.value,
                "tool_count": len(runtime.tools) if runtime else 0,
            })

        # Fallback: если конфиги не сохранены, но runtime уже есть.
        if not result:
            for name, runtime in self.owner.server_runtimes.items():
                result.append({
                    "name": name,
                    "alias": runtime.alias,
                    "enabled_in_config": True,
                    "connected": True,
                    "connect_type": runtime.connect_type.value,
                    "tool_count": len(runtime.tools),
                })

        return result

    def list_tools(
        self,
        server_names: list[str] | None = None,
        include_schemas: bool = False,
    ) -> list[dict[str, Any]]:
        allowed = set(server_names or [])

        tools: list[dict[str, Any]] = []

        for binding in self.owner.available_tools:
            if allowed:
                if (
                    binding.server_name not in allowed
                    and binding.server_alias not in allowed
                ):
                    continue

            item: dict[str, Any] = {
                "name": binding.public_name,
                "server": binding.server_name,
                "server_alias": binding.server_alias,
                "remote_name": binding.remote_name,
                "description": binding.description,
            }

            if include_schemas:
                item["input_schema"] = binding.input_schema

            tools.append(item)

        return tools

    def get_tool_schema(self, tool_name: str) -> dict[str, Any]:
        binding = self.owner.tool_registry.get(tool_name)

        if binding is None:
            raise ValueError(f"Unknown tool: {tool_name}")

        return {
            "name": binding.public_name,
            "server": binding.server_name,
            "server_alias": binding.server_alias,
            "remote_name": binding.remote_name,
            "description": binding.description,
            "input_schema": binding.input_schema,
        }

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ):
        return await self.owner._call_registered_tool(tool_name, arguments)

    async def enable_server(self, name: str) -> dict[str, Any]:
        config = self.owner.server_configs_by_name.get(name)

        if config is None:
            raise ValueError(f"Unknown server config: {name}")

        if name in self.owner.server_runtimes:
            return {"status": "already_connected", "server": name}

        config.enabled = True

        runtime = await self.owner._connect_single_server(config)
        self.owner.server_runtimes[runtime.name] = runtime
        self.owner._register_server_tools(runtime)

        return {"status": "connected", "server": name}

    async def disable_server(self, name: str) -> dict[str, Any]:
        runtime = self.owner.server_runtimes.pop(name, None)

        if runtime is None:
            return {"status": "already_disconnected", "server": name}

        await self.owner._close_runtime(runtime)
        self.owner._unregister_server_tools(name)

        config = self.owner.server_configs_by_name.get(name)
        if config is not None:
            config.enabled = False

        return {"status": "disconnected", "server": name}

    async def reload_server(self, name: str) -> dict[str, Any]:
        await self.disable_server(name)
        return await self.enable_server(name)