"""Standalone MCP client fixture for the dual-SDK wire matrix.

Runs under the isolated mcp==1.28.1 interpreter (``MCP_LEGACY_PYTHON``).
The server command is passed as argv — it may run under a DIFFERENT
interpreter (the main env's python for the served surfaces, or
``MCP_LEGACY_PYTHON`` for the legacy fixture server). Uses only the
``ClientSession``/``StdioServerParameters`` API surface, which is stable
across mcp 1.28.1 → 2.0.0.

Usage::

    mcp_legacy_client.py <tool_name> <server_cmd...>

Exits 0 on a successful initialize → tools/list → tools/call round-trip.
"""

import asyncio
import sys

from mcp import ClientSession, StdioServerParameters, stdio_client


async def _main(tool_name: str, server_argv) -> int:
    params = StdioServerParameters(command=server_argv[0], args=server_argv[1:])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            if tool_name not in names:
                print(
                    f"ERROR: {tool_name!r} missing from {sorted(names)}",
                    file=sys.stderr,
                )
                return 3
            call = await session.call_tool(tool_name, arguments={})
            text = "".join(
                getattr(block, "text", "") or ""
                for block in (getattr(call, "content", None) or [])
            )
            if getattr(call, "is_error", False) or getattr(call, "isError", False):
                print(f"ERROR: tool call failed: {text}", file=sys.stderr)
                return 4
            print(
                f"OK legacy-client {tool_name} "
                f"era={getattr(session, 'protocol_version', '?')} "
                f"result={text.strip()!r}"
            )
            return 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(
            "usage: mcp_legacy_client.py <tool_name> <server_cmd...>",
            file=sys.stderr,
        )
        sys.exit(2)
    tool = sys.argv[1]
    server_argv = sys.argv[2:]
    sys.exit(asyncio.run(_main(tool, server_argv)))
