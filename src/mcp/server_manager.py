from __future__ import annotations

import asyncio
import logging
from typing import Any, TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from .mcp_client import MCPClient


logger = logging.getLogger("mcp_client")


class MCPServerManagerError(RuntimeError):
    pass


class MCPToolNotFoundError(MCPServerManagerError):
    pass


class MCPServerNotConnectedError(MCPServerManagerError):
    pass


class MCPTransportLifecycleError(MCPServerManagerError):
    pass


class MCPServerConnectionError(MCPTransportLifecycleError):
    """Managed failure to establish an MCP server runtime."""

    def __init__(self, server_name: str, cause: BaseException):
        self.server_name = server_name
        self.cause_type = type(cause).__name__
        super().__init__(
            f"Failed to connect MCP server {server_name}: {self.cause_type}"
        )


class MCPServerRecoveryError(MCPServerManagerError):
    pass


class MCPToolCallFailedError(MCPServerManagerError):
    pass


class MCPServerManager:
    """
    Runtime-менеджер MCP-серверов.

    v0.3:
    - работает поверх текущего MCPClient;
    - не хранит собственные подключения;
    - координирует lifecycle, recovery и retry MCP runtime;
    - использует low-level connect/close/register helpers владельца.
    """

    def __init__(self, owner: "MCPClient"):
        self.owner = owner

    def list_servers(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []

        # Если connect_to_servers ещё не вызывался,
        # server_configs_by_name может быть пустым.
        for name, config in self.owner.server_configs_by_name.items():
            runtime = self.owner.server_runtimes.get(name)
            startup_error = getattr(
                self.owner,
                "server_startup_errors",
                {},
            ).get(name)

            result.append({
                "name": name,
                "alias": config.alias or "",
                "enabled_in_config": bool(config.enabled),
                "startup_required": bool(
                    getattr(config, "startup_required", True)
                ),
                "connected": runtime is not None,
                "connect_type": config.connect_type.value,
                "tool_count": len(runtime.tools) if runtime else 0,
                "healthy": bool(runtime.healthy) if runtime else False,
                "reconnecting": bool(runtime.reconnecting) if runtime else False,
                "generation": runtime.generation if runtime else None,
                "last_error": runtime.last_error if runtime else startup_error,
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
                    "healthy": runtime.healthy,
                    "reconnecting": runtime.reconnecting,
                    "generation": runtime.generation,
                    "last_error": runtime.last_error,
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
        binding = self.resolve_tool_binding(tool_name)

        return {
            "name": binding.public_name,
            "server": binding.server_name,
            "server_alias": binding.server_alias,
            "remote_name": binding.remote_name,
            "description": binding.description,
            "input_schema": binding.input_schema,
        }

    def resolve_tool_binding(self, tool_name: str):
        binding = self.owner.tool_registry.get(tool_name)

        if binding is None:
            raise MCPToolNotFoundError(f"Unknown tool: {tool_name}")

        return binding

    def get_runtime(self, server_name: str):
        runtime = self.owner.server_runtimes.get(server_name)

        if runtime is None:
            raise MCPServerNotConnectedError(
                f"Server is not connected: {server_name}"
            )

        return runtime

    def _get_reconnect_lock(self, server_name: str) -> asyncio.Lock:
        lock = self.owner.server_reconnect_locks.get(server_name)

        if lock is None:
            lock = asyncio.Lock()
            self.owner.server_reconnect_locks[server_name] = lock

        return lock

    def mark_unhealthy(self, runtime, error: BaseException | str) -> None:
        runtime.healthy = False
        runtime.last_error = (
            str(error)
            if isinstance(error, str)
            else f"{type(error).__name__}: {error!r}"
        )

    def is_transport_lifecycle_error(self, error: BaseException) -> bool:
        name = type(error).__name__
        text = f"{name}: {error!r}".lower()
        lifecycle_error_names = {
            "TimeoutError",
            "ReadError",
            "WriteError",
            "ConnectError",
            "ConnectTimeout",
            "ReadTimeout",
            "RemoteProtocolError",
            "PoolTimeout",
            "ClosedResourceError",
            "BrokenResourceError",
            "EndOfStream",
            "BrokenPipeError",
            "ConnectionResetError",
            "ConnectionAbortedError",
            "ConnectionRefusedError",
        }

        if isinstance(error, MCPTransportLifecycleError):
            return True
        if self.is_mcp_transport_cancellation(error):
            return True
        if name in lifecycle_error_names:
            return True
        if isinstance(error, (asyncio.TimeoutError, httpx.HTTPError)):
            return True
        if "closed" in text and "resource" in text:
            return True
        if "broken pipe" in text or "connection reset" in text:
            return True
        if "session terminated" in text:
            return True
        if "session closed" in text:
            return True
        if "session is closed" in text:
            return True
        if "session termination failed" in text:
            return True
        if "connection attempts failed" in text:
            return True
        if "attempted to exit cancel scope" in text:
            return True
        return False

    def is_mcp_transport_cancellation(self, error: BaseException) -> bool:
        if not isinstance(error, asyncio.CancelledError):
            return False

        # asyncio.CancelledError не является Exception в Python 3.11+.
        # Внутри MCP streamable HTTP он может означать не отмену HTTP-запроса
        # Gateway, а внутренний shutdown/cancel transport-а при initialize,
        # reconnect или call_tool. На этой lifecycle-boundary превращаем его
        # в управляемую MCP recovery/tool error вместо проброса наружу.
        return True

    def supports_recovery(self, runtime) -> bool:
        return runtime.connect_type.value in {
            "streamable_http",
            "http",
            "executable",
        }

    async def replace_runtime(self, server_name: str, new_runtime):
        old_runtime = self.owner.server_runtimes.get(server_name)
        old_generation = old_runtime.generation if old_runtime is not None else 0

        if old_runtime is not None:
            await self.owner._close_runtime(
                old_runtime,
                reason="runtime_replace",
            )

        new_runtime.generation = old_generation + 1
        new_runtime.healthy = True
        new_runtime.reconnecting = False
        new_runtime.last_error = None

        self.owner.server_runtimes[server_name] = new_runtime
        getattr(self.owner, "server_startup_errors", {}).pop(
            server_name,
            None,
        )
        self.owner._unregister_server_tools(server_name)
        self.owner._register_server_tools(new_runtime)
        return new_runtime

    async def recover_runtime(self, server_name: str):
        config = self.owner.server_configs_by_name.get(server_name)
        if config is None:
            raise MCPServerRecoveryError(
                f"No server config for recovery: {server_name}"
            )
        if not config.enabled:
            raise MCPServerRecoveryError(
                f"Server is disabled; recovery is not allowed: {server_name}"
            )

        lock = self._get_reconnect_lock(server_name)

        async with lock:
            current = self.owner.server_runtimes.get(server_name)
            if current is not None and current.healthy:
                return current

            if current is not None:
                current.reconnecting = True

            logger.warning("Recovering MCP server runtime: %s", server_name)

            try:
                async with asyncio.timeout(
                    self.owner.mcp_reconnect_timeout
                ):
                    new_runtime = await self.owner._connect_single_server(
                        config
                    )
                new_runtime = await self.replace_runtime(
                    server_name,
                    new_runtime,
                )
                logger.info(
                    "MCP server runtime recovered: %s generation=%s",
                    server_name,
                    new_runtime.generation,
                )
                return new_runtime
            except BaseException as e:
                if not self.is_transport_lifecycle_error(e):
                    raise
                if current is not None:
                    self.mark_unhealthy(current, e)
                    current.reconnecting = False
                raise MCPServerRecoveryError(
                    f"Failed to recover MCP server {server_name}: "
                    f"{type(e).__name__}: {e!r}"
                ) from e

    async def call_tool_once(
        self,
        binding,
        arguments: dict[str, Any],
        *,
        runtime=None,
    ):
        runtime = runtime or self.get_runtime(binding.server_name)

        if runtime.session is not None:
            return await runtime.session.call_tool(
                binding.remote_name,
                arguments,
            )
        if runtime.http_client is not None:
            return await runtime.http_client.call_tool(
                binding.remote_name,
                arguments,
            )
        raise MCPTransportLifecycleError(
            f"Server {binding.server_name} has no active client"
        )

    async def call_tool_with_recovery(
        self,
        binding,
        arguments: dict[str, Any],
    ):
        last_error: BaseException | None = None
        total_attempts = self.owner.mcp_call_retries_after_recovery + 1

        for attempt in range(1, total_attempts + 1):
            runtime = self.get_runtime(binding.server_name)

            if not runtime.healthy:
                if not self.supports_recovery(runtime):
                    raise MCPServerNotConnectedError(
                        "Server runtime is unhealthy and recovery is not "
                        f"supported: {binding.server_name}"
                    )
                try:
                    runtime = await self.recover_runtime(binding.server_name)
                except BaseException as recovery_error:
                    if not (
                        isinstance(recovery_error, MCPServerManagerError)
                        or self.is_transport_lifecycle_error(recovery_error)
                    ):
                        raise
                    last_error = recovery_error
                    break

            try:
                return await asyncio.wait_for(
                    self.call_tool_once(
                        binding,
                        arguments,
                        runtime=runtime,
                    ),
                    timeout=self.owner.mcp_transport_call_timeout,
                )
            except BaseException as e:
                last_error = e

                if not self.is_transport_lifecycle_error(e):
                    raise

                # Помечаем именно runtime этой попытки: поздняя ошибка старого
                # поколения не должна испортить уже восстановленный runtime.
                self.mark_unhealthy(runtime, e)

                logger.warning(
                    "MCP lifecycle error: tool=%s server=%s attempt=%s/%s "
                    "generation=%s error=%s: %r",
                    binding.public_name,
                    binding.server_name,
                    attempt,
                    total_attempts,
                    runtime.generation,
                    type(e).__name__,
                    e,
                )

                if attempt >= total_attempts:
                    break

                try:
                    await self.recover_runtime(binding.server_name)
                except BaseException as recovery_error:
                    if not (
                        isinstance(recovery_error, MCPServerManagerError)
                        or self.is_transport_lifecycle_error(recovery_error)
                    ):
                        raise
                    last_error = recovery_error
                    logger.warning(
                        "MCP runtime recovery failed: server=%s error=%s: %r",
                        binding.server_name,
                        type(recovery_error).__name__,
                        recovery_error,
                    )
                    break

        raise MCPToolCallFailedError(
            "MCP tool call failed after recovery attempts: "
            f"tool={binding.public_name}, server={binding.server_name}, "
            f"last_error={type(last_error).__name__}: {last_error!r}"
        )

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ):
        binding = self.resolve_tool_binding(tool_name)
        return await self.call_tool_with_recovery(binding, arguments)

    async def enable_server(self, name: str) -> dict[str, Any]:
        config = self.owner.server_configs_by_name.get(name)

        if config is None:
            raise ValueError(f"Unknown server config: {name}")

        if name in self.owner.server_runtimes:
            return {"status": "already_connected", "server": name}

        config.enabled = True

        runtime = await self.owner._connect_single_server(config)
        await self.replace_runtime(runtime.name, runtime)
        getattr(self.owner, "server_startup_errors", {}).pop(name, None)

        return {"status": "connected", "server": name}

    async def disable_server(self, name: str) -> dict[str, Any]:
        config = self.owner.server_configs_by_name.get(name)
        if config is not None:
            config.enabled = False

        runtime = self.owner.server_runtimes.pop(name, None)
        getattr(self.owner, "server_startup_errors", {}).pop(name, None)
        self.owner._unregister_server_tools(name)

        if runtime is None:
            return {"status": "already_disconnected", "server": name}

        await self.owner._close_runtime(runtime, reason="server_disable")

        return {"status": "disconnected", "server": name}

    async def reload_server(self, name: str) -> dict[str, Any]:
        config = self.owner.server_configs_by_name.get(name)
        if config is None:
            raise ValueError(f"Unknown server config: {name}")

        config.enabled = True
        runtime = await self.owner._connect_single_server(config)
        await self.replace_runtime(name, runtime)
        getattr(self.owner, "server_startup_errors", {}).pop(name, None)
        return {"status": "reloaded", "server": name}
