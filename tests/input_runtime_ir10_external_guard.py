"""Pytest plugin that proves IR-10 automated acceptance has no external calls.

The plugin mirrors the guard style used by the existing v0.4 transport/artifact
roast. It is loaded explicitly only for the focused IR-10 and seeded release
roast commands; normal repository tests are not globally monkeypatched.
"""

from __future__ import annotations

import inspect
import json
import os
import socket
from unittest.mock import patch


COUNTERS = {
    "llm": 0,
    "mcp_network": 0,
    "telegram": 0,
    "web_external": 0,
    "internet": 0,
    "agent_cycle": 0,
}
PATCHERS = []


def _blocked(counter: str, label: str):
    async def blocked_async(*args, **kwargs):
        COUNTERS[counter] += 1
        raise AssertionError(label)

    return blocked_async


def pytest_configure(config):
    original_connect = socket.socket.connect

    def guarded_connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            # Windows asyncio can implement socketpair with a private loopback
            # TCP connection. Permit only that stdlib-internal connection.
            if any(
                frame.function == "socketpair"
                and os.path.basename(frame.filename) == "socket.py"
                for frame in inspect.stack()
            ):
                return original_connect(sock, address)
            COUNTERS["internet"] += 1
            COUNTERS["web_external"] += 1
            raise AssertionError(f"IR-10 external network is forbidden: {address!r}")
        return original_connect(sock, address)

    PATCHERS.append(patch.object(socket.socket, "connect", guarded_connect))

    # Import only modules that do not instantiate the global API/configuration
    # shell as an import side effect. The actual Agent/LLM/MCP/Telegram/network
    # boundaries remain fenced; production runtime composition is exercised by
    # the ordinary IR-8/IR-9/full-repository gates without real credentials.
    from src.api.artifact_transport import ArtifactTransportFacade
    from src.mcp.mcp_client import MCPClient
    from src.mcp.server_manager import MCPServerManager

    PATCHERS.extend(
        [
            patch.object(
                ArtifactTransportFacade,
                "run_committed_batch",
                _blocked("agent_cycle", "real AgentCycle path is forbidden in automated IR-10"),
            ),
            patch.object(
                MCPClient,
                "_call_llm",
                _blocked("llm", "real LLM call is forbidden in automated IR-10"),
            ),
            patch.object(
                MCPClient,
                "connect_to_servers",
                _blocked("mcp_network", "real MCP connection is forbidden in automated IR-10"),
            ),
            patch.object(
                MCPServerManager,
                "call_tool",
                _blocked("mcp_network", "real MCP tool call is forbidden in automated IR-10"),
            ),
        ]
    )
    try:
        from telegram import Bot

        PATCHERS.append(
            patch.object(
                Bot,
                "_post",
                _blocked("telegram", "real Telegram call is forbidden in automated IR-10"),
            )
        )
    except Exception:
        pass

    for item in PATCHERS:
        item.start()


def pytest_sessionfinish(session, exitstatus):
    print("IR10_EXTERNAL_CALLS=" + json.dumps(COUNTERS, sort_keys=True))
    if any(COUNTERS.values()):
        session.exitstatus = 1


def pytest_unconfigure(config):
    for item in reversed(PATCHERS):
        item.stop()
    PATCHERS.clear()
