"""Minimal FastMCP legacy-era server fixture for the dual-SDK wire matrix.

Runs ONLY under the isolated mcp==1.28.1 venv (``MCP_LEGACY_PYTHON``):
``mcp.server.fastmcp`` was removed in mcp 2.0, so this file must never be
imported by the main env. Exposes one arg-less tool for side-effect-free
probing from either era client.
"""

import sys

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("legacy-fixture")


@mcp.tool()
def ping_echo() -> str:
    """Arg-less echo tool — safe to call from either era client."""
    return "pong"


if __name__ == "__main__":
    mcp.run()
