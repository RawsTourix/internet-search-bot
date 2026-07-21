"""Deterministic MCP tools for the v0.4 DAG-planning live smoke test.

The server intentionally has no network or filesystem side effects. It gives the
real agent a small multi-step task whose result is stable across runs.
"""

from __future__ import annotations

import asyncio
import json

from mcp.server.fastmcp import FastMCP


mcp = FastMCP(name="dag-planning-live-smoke")


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


@mcp.tool()
async def smoke_get_alpha() -> str:
    """Return the deterministic alpha value for the DAG-planning smoke test."""
    return _json({
        "type": "smoke_value",
        "name": "alpha",
        "value": 17,
    })


@mcp.tool()
async def smoke_get_beta() -> str:
    """Return the deterministic beta value for the DAG-planning smoke test."""
    return _json({
        "type": "smoke_value",
        "name": "beta",
        "value": 25,
    })


@mcp.tool()
async def smoke_verify_total(total: int) -> str:
    """Verify that the supplied total equals the deterministic expected value."""
    expected = 42
    return _json({
        "type": "smoke_verification",
        "expected": expected,
        "received": int(total),
        "valid": int(total) == expected,
    })


async def main() -> None:
    await mcp.run_stdio_async()


if __name__ == "__main__":
    asyncio.run(main())
