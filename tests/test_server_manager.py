import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx

from src.mcp.mcp_client import (
    MCPClient,
    MCPServerRuntime,
    MCPToolBinding,
    ServerConnectType,
)
from src.mcp.server_manager import (
    MCPServerManager,
    MCPToolCallFailedError,
    MCPToolNotFoundError,
)


def make_runtime(*, session, healthy=True, generation=0):
    return MCPServerRuntime(
        name="demo",
        alias="",
        connect_type=ServerConnectType.STREAMABLE_HTTP,
        session=session,
        healthy=healthy,
        generation=generation,
    )


def make_binding():
    return MCPToolBinding(
        public_name="search",
        server_name="demo",
        server_alias="",
        remote_name="search_remote",
        description="",
        input_schema={},
    )


def make_owner(runtime):
    binding = make_binding()
    owner = SimpleNamespace(
        server_runtimes={"demo": runtime},
        tool_registry={"search": binding},
        available_tools=[binding],
        server_configs_by_name={
            "demo": SimpleNamespace(enabled=True, name="demo")
        },
        server_reconnect_locks={},
        mcp_transport_call_timeout=1.0,
        mcp_reconnect_timeout=1.0,
        mcp_call_retries_after_recovery=1,
        _connect_single_server=AsyncMock(),
        _close_runtime=AsyncMock(),
        _unregister_server_tools=Mock(),
        _register_server_tools=Mock(),
    )
    return owner, binding


class MCPServerManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_stable_tool_call_uses_existing_runtime(self):
        expected = SimpleNamespace(content=[])
        session = SimpleNamespace(call_tool=AsyncMock(return_value=expected))
        runtime = make_runtime(session=session)
        owner, _ = make_owner(runtime)
        manager = MCPServerManager(owner)

        result = await manager.call_tool("search", {"q": "python"})

        self.assertIs(result, expected)
        session.call_tool.assert_awaited_once_with(
            "search_remote",
            {"q": "python"},
        )
        owner._connect_single_server.assert_not_awaited()

    async def test_transport_error_recovers_and_retries_once(self):
        stale_session = SimpleNamespace(
            call_tool=AsyncMock(side_effect=httpx.ReadError("stale session"))
        )
        old_runtime = make_runtime(session=stale_session)
        owner, _ = make_owner(old_runtime)

        expected = SimpleNamespace(content=[])
        new_session = SimpleNamespace(call_tool=AsyncMock(return_value=expected))
        new_runtime = make_runtime(session=new_session)
        owner._connect_single_server.return_value = new_runtime
        manager = MCPServerManager(owner)

        result = await manager.call_tool("search", {})

        self.assertIs(result, expected)
        self.assertFalse(old_runtime.healthy)
        self.assertEqual(new_runtime.generation, 1)
        self.assertTrue(new_runtime.healthy)
        owner._connect_single_server.assert_awaited_once()
        owner._close_runtime.assert_awaited_once_with(old_runtime)
        new_session.call_tool.assert_awaited_once()

    async def test_session_terminated_text_recovers_and_retries_once(self):
        stale_session = SimpleNamespace(
            call_tool=AsyncMock(side_effect=RuntimeError("Session terminated"))
        )
        old_runtime = make_runtime(session=stale_session)
        owner, _ = make_owner(old_runtime)

        expected = SimpleNamespace(content=[])
        new_session = SimpleNamespace(call_tool=AsyncMock(return_value=expected))
        new_runtime = make_runtime(session=new_session)
        owner._connect_single_server.return_value = new_runtime
        manager = MCPServerManager(owner)

        result = await manager.call_tool("search", {})

        self.assertIs(result, expected)
        self.assertFalse(old_runtime.healthy)
        self.assertEqual(new_runtime.generation, 1)
        owner._connect_single_server.assert_awaited_once()
        new_session.call_tool.assert_awaited_once()

    async def test_cancelled_tool_call_is_lifecycle_error_not_task_escape(self):
        async def raise_cancelled(*_args, **_kwargs):
            raise asyncio.CancelledError("session cancelled")

        stale_session = SimpleNamespace(call_tool=raise_cancelled)
        old_runtime = make_runtime(session=stale_session)
        owner, _ = make_owner(old_runtime)

        expected = SimpleNamespace(content=[])
        new_session = SimpleNamespace(call_tool=AsyncMock(return_value=expected))
        new_runtime = make_runtime(session=new_session)
        owner._connect_single_server.return_value = new_runtime
        manager = MCPServerManager(owner)

        result = await manager.call_tool("search", {})

        self.assertIs(result, expected)
        self.assertFalse(old_runtime.healthy)
        self.assertEqual(new_runtime.generation, 1)
        new_session.call_tool.assert_awaited_once()

    async def test_cancelled_recovery_becomes_tool_call_failure(self):
        stale_session = SimpleNamespace(
            call_tool=AsyncMock(side_effect=httpx.ReadError("stale session"))
        )
        runtime = make_runtime(session=stale_session)
        owner, _ = make_owner(runtime)

        async def raise_cancelled(_config):
            raise asyncio.CancelledError("session initialize cancelled")

        owner._connect_single_server.side_effect = raise_cancelled
        manager = MCPServerManager(owner)

        with self.assertRaises(MCPToolCallFailedError) as raised:
            await manager.call_tool("search", {})

        self.assertIn("tool=search", str(raised.exception))
        self.assertIn("MCPServerRecoveryError", str(raised.exception))
        self.assertFalse(runtime.healthy)
        self.assertFalse(runtime.reconnecting)

    async def test_application_error_does_not_reconnect_or_mark_unhealthy(self):
        session = SimpleNamespace(
            call_tool=AsyncMock(side_effect=ValueError("invalid arguments"))
        )
        runtime = make_runtime(session=session)
        owner, _ = make_owner(runtime)
        manager = MCPServerManager(owner)

        with self.assertRaisesRegex(ValueError, "invalid arguments"):
            await manager.call_tool("search", {})

        self.assertTrue(runtime.healthy)
        self.assertIsNone(runtime.last_error)
        owner._connect_single_server.assert_not_awaited()

    async def test_unknown_tool_fails_without_recovery(self):
        runtime = make_runtime(
            session=SimpleNamespace(call_tool=AsyncMock())
        )
        owner, _ = make_owner(runtime)
        manager = MCPServerManager(owner)

        with self.assertRaises(MCPToolNotFoundError):
            await manager.call_tool("missing", {})

        owner._connect_single_server.assert_not_awaited()

    async def test_recovery_failure_returns_technical_manager_error(self):
        stale_session = SimpleNamespace(
            call_tool=AsyncMock(side_effect=httpx.ReadError("stale session"))
        )
        runtime = make_runtime(session=stale_session)
        owner, _ = make_owner(runtime)
        owner._connect_single_server.side_effect = httpx.ConnectError(
            "server is down"
        )
        manager = MCPServerManager(owner)

        with self.assertRaises(MCPToolCallFailedError) as raised:
            await manager.call_tool("search", {})

        self.assertIn("tool=search", str(raised.exception))
        self.assertIn("MCPServerRecoveryError", str(raised.exception))
        self.assertFalse(runtime.healthy)

    async def test_mcp_client_routes_real_tool_to_server_manager(self):
        expected = SimpleNamespace(content=[])
        client = object.__new__(MCPClient)
        client.manager_tools = {}
        client.server_manager = SimpleNamespace(
            call_tool=AsyncMock(return_value=expected)
        )

        result = await client._call_registered_tool("search", {"q": "python"})

        self.assertIs(result, expected)
        client.server_manager.call_tool.assert_awaited_once_with(
            "search",
            {"q": "python"},
        )

    async def test_parallel_calls_share_single_recovery(self):
        stale_runtime = make_runtime(
            session=SimpleNamespace(call_tool=AsyncMock()),
            healthy=False,
        )
        owner, _ = make_owner(stale_runtime)

        expected = SimpleNamespace(content=[])
        new_session = SimpleNamespace(call_tool=AsyncMock(return_value=expected))
        new_runtime = make_runtime(session=new_session)

        async def connect(_config):
            await asyncio.sleep(0)
            return new_runtime

        owner._connect_single_server.side_effect = connect
        manager = MCPServerManager(owner)

        first, second = await asyncio.gather(
            manager.call_tool("search", {"request": 1}),
            manager.call_tool("search", {"request": 2}),
        )

        self.assertIs(first, expected)
        self.assertIs(second, expected)
        self.assertEqual(owner._connect_single_server.await_count, 1)
        self.assertEqual(new_session.call_tool.await_count, 2)
        self.assertEqual(new_runtime.generation, 1)


if __name__ == "__main__":
    unittest.main()
