"""Dual-SDK wire conformance tests (#88698 R4/R5).

Runs in the dedicated ``mcp-dual-era-conformance`` CI job: the main env
carries ``mcp==2.0.0`` and ``MCP_LEGACY_PYTHON`` points at an isolated
``mcp==1.28.1`` venv (built by the workflow). These tests exercise REAL
legacy (1.28.1) and REAL modern (2.0.0) wires against both served surfaces
(``mcp_serve`` and ``agent.transports.hermes_tools_mcp_server``) plus the
isolated legacy fixture server, and assert the observable negotiated state
exposed by ``get_mcp_status()``'s protocol sub-dict.

Skips cleanly everywhere else: the general sliced lane has no
``MCP_LEGACY_PYTHON`` (and honors ``-m 'not integration'``), so these tests
are no-ops there by design.
"""

import asyncio
import json
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LEGACY_FIXTURE_SERVER = os.path.join(REPO_ROOT, "tests", "fixtures", "mcp_legacy_server.py")
LEGACY_FIXTURE_CLIENT = os.path.join(REPO_ROOT, "tests", "fixtures", "mcp_legacy_client.py")


@pytest.fixture(scope="module", autouse=True)
def _dual_sdk_required(tmp_path_factory):
    """Skip unless the dedicated dual-SDK job is present (MCP_LEGACY_PYTHON)."""
    legacy_python = os.environ.get("MCP_LEGACY_PYTHON") or ""
    if not legacy_python or not os.path.exists(legacy_python):
        pytest.skip(
            "dual-SDK wire tests require MCP_LEGACY_PYTHON (mcp==1.28.1 venv); "
            "see .github/workflows/mcp-dual-era-conformance.yml"
        )
    # Isolate HERMES_HOME so spawned mcp_serve processes touch nothing real.
    hermes_home = tmp_path_factory.mktemp("wire-hermes-home")
    os.environ["HERMES_HOME"] = str(hermes_home)
    return legacy_python


def _main_python() -> str:
    """The interpreter running this test (the repo env, mcp==2.0.0)."""
    return sys.executable


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# No-initialize rejection (pinned JSON-RPC error)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "server_argv,label",
    [
        (
            [sys.executable, "-c", "import mcp_serve; mcp_serve.run_mcp_server()"],
            "mcp_serve",
        ),
        (
            [sys.executable, "-m", "agent.transports.hermes_tools_mcp_server"],
            "hermes-tools",
        ),
    ],
    ids=["mcp_serve", "hermes-tools"],
)
def test_no_initialize_rejected(server_argv, label):
    """A bare ``tools/list`` without ``initialize`` is refused with -32602.

    Pins the exact JSON-RPC error both dual-era served surfaces emit for an
    un-initialized legacy-shaped request (#88698 R4/R5). The 2026-07-28 era
    classifies the FIRST request: a non-initialize, non-enveloped request
    never reaches the tools kernel.
    """
    proc = subprocess.Popen(
        server_argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**os.environ, "HERMES_QUIET": "1"},
    )
    try:
        req = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        proc.stdin.write((json.dumps(req) + "\n").encode())
        proc.stdin.flush()
        line = proc.stdout.readline()
        assert line, f"{label}: server exited without a response"
        resp = json.loads(line.decode("utf-8"))
        assert resp.get("id") == 1
        error = resp.get("error") or {}
        assert error.get("code") == -32602, f"{label}: unexpected error {error}"
        assert "Invalid request parameters" in error.get("message", "")
    finally:
        proc.kill()
        proc.wait()


# ---------------------------------------------------------------------------
# Modern (2.0.0) client ↔ legacy (1.28.1) fixture server
# ---------------------------------------------------------------------------

def test_modern_client_legacy_fixture_server(_dual_sdk_required):
    """A mcp 2.0.0 client initializes and calls tools on a 1.28.1 server."""
    from mcp import ClientSession, StdioServerParameters, stdio_client

    legacy_python = _dual_sdk_required
    params = StdioServerParameters(
        command=legacy_python, args=[LEGACY_FIXTURE_SERVER]
    )

    async def drive():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                assert getattr(init, "protocol_version", None) == "2025-11-25"
                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                assert "ping_echo" in names
                call = await session.call_tool("ping_echo", arguments={})
                text = "".join(
                    getattr(b, "text", "") or ""
                    for b in (getattr(call, "content", None) or [])
                )
                assert text.strip() == "pong"

    _run(drive())


# ---------------------------------------------------------------------------
# Legacy (1.28.1) client ↔ modern (2.0.0) served surfaces
# ---------------------------------------------------------------------------

def test_legacy_client_modern_hermes_tools_surface(_dual_sdk_required):
    """The 1.28.1 fixture client speaks to the 2.0 hermes-tools surface.

    Uses ``skills_list`` — a read-only, arg-less Hermes tool with no side
    effects (never ``web_search`` etc.).
    """
    legacy_python = _dual_sdk_required
    proc = subprocess.run(
        [
            legacy_python, LEGACY_FIXTURE_CLIENT,
            "skills_list",
            _main_python(), "-m", "agent.transports.hermes_tools_mcp_server",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "HERMES_QUIET": "1", "HERMES_HOME": os.environ.get("HERMES_HOME", "")},
    )
    assert proc.returncode == 0, f"legacy client failed:\n{proc.stdout}\n{proc.stderr}"
    assert "OK legacy-client skills_list" in proc.stdout


def test_legacy_client_legacy_fixture_server(_dual_sdk_required):
    """Legacy client ↔ legacy fixture server, both under the 1.28.1 venv."""
    legacy_python = _dual_sdk_required
    proc = subprocess.run(
        [
            legacy_python, LEGACY_FIXTURE_CLIENT,
            "ping_echo",
            legacy_python, LEGACY_FIXTURE_SERVER,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "HERMES_HOME": os.environ.get("HERMES_HOME", "")},
    )
    assert proc.returncode == 0, f"legacy client failed:\n{proc.stdout}\n{proc.stderr}"
    assert "OK legacy-client ping_echo" in proc.stdout
    assert "pong" in proc.stdout


# ---------------------------------------------------------------------------
# Negotiated-state observability across real legacy + stateless wires
# ---------------------------------------------------------------------------

def test_negotiated_state_observable_after_legacy_and_stateless_wires(
    _dual_sdk_required, monkeypatch
):
    """#88698 R5: negotiated-era state lands in get_mcp_status()'s protocol
    sub-dict after REAL legacy-wire and REAL stateless-wire connects (plus a
    cross-era fallback leg), via the client-side surface (MCPServerTask
    ``_negotiate_session`` on real SDK sessions).
    """
    from mcp import ClientSession, StdioServerParameters, stdio_client

    import tools.mcp_tool as mcp_tool

    legacy_python = _dual_sdk_required
    legacy_params = StdioServerParameters(
        command=legacy_python, args=[LEGACY_FIXTURE_SERVER]
    )
    modern_params = StdioServerParameters(
        command=_main_python(),
        args=["-c", "import mcp_serve; mcp_serve.run_mcp_server()"],
    )

    results: dict = {}

    async def connect_legacy_wire():
        async with stdio_client(legacy_params) as (read, write):
            async with ClientSession(read, write) as session:
                task = mcp_tool.MCPServerTask("wire-legacy")
                task._config = {"protocol": "legacy", "command": "x"}
                out = await task._negotiate_session(session, 30)
                assert out is not None
                task.session = session
                results["legacy"] = task

    async def connect_stateless_wire():
        async with stdio_client(modern_params) as (read, write):
            async with ClientSession(read, write) as session:
                task = mcp_tool.MCPServerTask("wire-stateless")
                task._config = {"protocol": "prefer-modern", "command": "x"}
                out = await task._negotiate_session(session, 30)
                assert out is not None
                task.session = session
                results["stateless"] = task

    async def connect_cross_era_fallback():
        # prefer-modern client against a LEGACY (1.28.1) server: discover is
        # rejected with the exact -32601 signal → recorded fallback.
        async with stdio_client(legacy_params) as (read, write):
            async with ClientSession(read, write) as session:
                task = mcp_tool.MCPServerTask("wire-cross-era")
                task._config = {"protocol": "prefer-modern", "command": "x"}
                out = await task._negotiate_session(session, 30)
                assert out is not None
                task.session = session
                results["cross-era"] = task

    _run(connect_legacy_wire())
    _run(connect_stateless_wire())
    _run(connect_cross_era_fallback())

    monkeypatch.setattr(
        mcp_tool, "_load_mcp_config",
        lambda: {
            "wire-legacy": {"command": "x"},
            "wire-stateless": {"command": "x"},
            "wire-cross-era": {"command": "x"},
        },
    )
    with mcp_tool._lock:
        saved_servers = dict(mcp_tool._servers)
        mcp_tool._servers.clear()
        mcp_tool._servers.update(results)

    try:
        statuses = {e["name"]: e for e in mcp_tool.get_mcp_status()}
    finally:
        with mcp_tool._lock:
            mcp_tool._servers.clear()
            mcp_tool._servers.update(saved_servers)

    legacy_proto = statuses["wire-legacy"]["protocol"]
    assert legacy_proto["policy"] == "legacy"
    assert legacy_proto["negotiated_era"] == "legacy"
    assert legacy_proto["negotiated_protocol_version"] == "2025-11-25"
    assert legacy_proto["fallback_reason"] == "none"
    assert legacy_proto["connection_generation"] >= 1
    assert legacy_proto["liveness_strategy"] == "none"
    assert legacy_proto["subscription_state"] == "none"
    assert legacy_proto["cache_state"] == "none"
    assert legacy_proto["server_identity"] is not None

    stateless_proto = statuses["wire-stateless"]["protocol"]
    assert stateless_proto["policy"] == "prefer-modern"
    assert stateless_proto["negotiated_era"] == "stateless"
    assert stateless_proto["negotiated_protocol_version"] == "2026-07-28"
    assert stateless_proto["fallback_reason"] == "none"

    cross_era_proto = statuses["wire-cross-era"]["protocol"]
    assert cross_era_proto["policy"] == "prefer-modern"
    assert cross_era_proto["negotiated_era"] == "legacy"
    assert cross_era_proto["fallback_reason"] == "discover_rejected"
