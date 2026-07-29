import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest


SERVER = "lazy-catalog-test"
REMOTE_TOOL = "lookup"
REGISTRY_TOOL = "mcp__lazy_catalog_test__lookup"


def _mcp_tool(
    *,
    description="Look up documentation",
    output_schema=None,
    annotations=None,
):
    return SimpleNamespace(
        name=REMOTE_TOOL,
        description=description,
        inputSchema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
        outputSchema=output_schema,
        annotations=annotations,
    )


def _catalog_entry(*, description="Look up documentation"):
    return {
        "registry_name": REGISTRY_TOOL,
        "remote_name": REMOTE_TOOL,
        "kind": "tool",
        "schema": {
            "name": REGISTRY_TOOL,
            "description": description,
            "parameters": _mcp_tool(description=description).inputSchema,
        },
    }


def _config():
    return {
        "enabled": True,
        "lazy_connect": True,
        "url": "http://127.0.0.1:65530/mcp",
        "tools": {"resources": False, "prompts": False},
        "connect_timeout": 1,
    }


class _FakeServer:
    def __init__(self, *, description="Look up documentation"):
        self.name = SERVER
        self._tools = [_mcp_tool(description=description)]
        self.tool_timeout = 1
        self.session = object()
        self.initialize_result = None
        self.shutdown_calls = 0

    async def shutdown(self):
        self.shutdown_calls += 1


@pytest.fixture(autouse=True)
def _clean_lazy_state():
    from tools import mcp_tool
    from tools.registry import registry

    registry.deregister(REGISTRY_TOOL)
    mcp_tool._lazy_server_configs.pop(SERVER, None)
    mcp_tool._lazy_server_catalogs.pop(SERVER, None)
    mcp_tool._lazy_registered_names.pop(SERVER, None)
    mcp_tool._lazy_connect_failures.pop(SERVER, None)
    mcp_tool._lazy_connect_locks.pop(SERVER, None)
    with mcp_tool._lock:
        mcp_tool._servers.pop(SERVER, None)
    yield
    registry.deregister(REGISTRY_TOOL)
    mcp_tool._lazy_server_configs.pop(SERVER, None)
    mcp_tool._lazy_server_catalogs.pop(SERVER, None)
    mcp_tool._lazy_registered_names.pop(SERVER, None)
    mcp_tool._lazy_connect_failures.pop(SERVER, None)
    mcp_tool._lazy_connect_locks.pop(SERVER, None)
    with mcp_tool._lock:
        mcp_tool._servers.pop(SERVER, None)


def _write_catalog():
    from tools.mcp_static_catalog import build_catalog, write_catalog

    catalog = build_catalog(SERVER, [_catalog_entry()])
    write_catalog(catalog)
    return catalog


def test_lazy_registration_uses_static_catalog_without_connecting(monkeypatch):
    from tools import mcp_tool
    from tools.registry import registry

    _write_catalog()
    monkeypatch.setattr(mcp_tool, "_MCP_AVAILABLE", True)

    def _must_not_start_loop():
        raise AssertionError("lazy registration started the MCP loop")

    monkeypatch.setattr(mcp_tool, "_ensure_mcp_loop", _must_not_start_loop)
    registered = mcp_tool.register_mcp_servers({SERVER: _config()})

    assert REGISTRY_TOOL in registered
    assert registry.get_toolset_for_tool(REGISTRY_TOOL) == f"mcp-{SERVER}"
    assert SERVER not in mcp_tool._servers
    assert SERVER in mcp_tool._lazy_server_configs


def test_lazy_catalog_is_internal_but_not_model_visible(monkeypatch):
    import model_tools
    from tools import mcp_tool

    _write_catalog()
    monkeypatch.setattr(mcp_tool, "_MCP_AVAILABLE", True)
    mcp_tool.register_mcp_servers({SERVER: _config()})

    raw = model_tools.get_tool_definitions(
        enabled_toolsets=[f"mcp-{SERVER}"],
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )
    visible = model_tools.get_tool_definitions(
        enabled_toolsets=[f"mcp-{SERVER}"],
        quiet_mode=True,
    )
    raw_names = {item["function"]["name"] for item in raw}
    visible_names = {item["function"]["name"] for item in visible}

    assert REGISTRY_TOOL in raw_names
    assert REGISTRY_TOOL not in visible_names
    assert {"tool_search", "tool_describe", "tool_call"} <= visible_names


def test_shutdown_removes_lazy_placeholders_and_state(monkeypatch):
    from tools import mcp_tool
    from tools.registry import registry

    _write_catalog()
    monkeypatch.setattr(mcp_tool, "_MCP_AVAILABLE", True)
    mcp_tool.register_mcp_servers({SERVER: _config()})

    mcp_tool.shutdown_mcp_servers()

    assert REGISTRY_TOOL not in registry.get_all_tool_names()
    assert SERVER not in mcp_tool._lazy_server_catalogs
    assert SERVER not in mcp_tool._lazy_server_configs
    assert SERVER not in mcp_tool._lazy_registered_names


def test_transport_outage_preserves_static_catalog_placeholder(monkeypatch):
    from tools import mcp_tool
    from tools.registry import registry

    _write_catalog()
    monkeypatch.setattr(mcp_tool, "_MCP_AVAILABLE", True)
    mcp_tool.register_mcp_servers({SERVER: _config()})
    task = mcp_tool.MCPServerTask(SERVER)
    task._static_catalog_generation = "sha256:pinned"
    task._registered_tool_names = [REGISTRY_TOOL]

    task._deregister_tools()

    assert REGISTRY_TOOL in registry.get_all_tool_names()
    assert task._registered_tool_names == [REGISTRY_TOOL]


def test_session_generation_rejects_replaced_registry_handler(monkeypatch):
    import model_tools
    from tools import mcp_tool
    from tools.mcp_static_catalog import build_catalog, write_catalog

    catalog_a = _write_catalog()
    monkeypatch.setattr(mcp_tool, "_MCP_AVAILABLE", True)
    mcp_tool.register_mcp_servers({SERVER: _config()})
    snapshot_a = model_tools.get_tool_definitions(
        enabled_toolsets=[f"mcp-{SERVER}"],
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )

    catalog_b = build_catalog(
        SERVER,
        [{
            "registry_name": REGISTRY_TOOL,
            "remote_name": REMOTE_TOOL,
            "kind": "tool",
            "schema": {
                "name": REGISTRY_TOOL,
                "description": "Generation B",
                "parameters": _mcp_tool().inputSchema,
            },
        }],
    )
    write_catalog(catalog_b)
    mcp_tool.register_mcp_servers({SERVER: _config()})

    async def _must_not_connect(name, config):
        raise AssertionError("stale session attempted to connect generation B")

    monkeypatch.setattr(mcp_tool, "_connect_server", _must_not_connect)
    result = json.loads(model_tools.handle_function_call(
        function_name="tool_call",
        function_args={"name": REGISTRY_TOOL, "arguments": {"query": "x"}},
        enabled_toolsets=[f"mcp-{SERVER}"],
        deferred_tool_defs=snapshot_a,
        deferred_tool_generations={REGISTRY_TOOL: catalog_a["generation"]},
    ))

    assert result["error_type"] == "mcp_catalog_stale"
    assert "new session" in result["error"].lower()


def test_lazy_server_without_catalog_stays_disconnected_and_unregistered(monkeypatch):
    from tools import mcp_tool
    from tools.registry import registry

    monkeypatch.setattr(mcp_tool, "_MCP_AVAILABLE", True)
    monkeypatch.setattr(
        mcp_tool,
        "_ensure_mcp_loop",
        lambda: (_ for _ in ()).throw(AssertionError("MCP loop started")),
    )

    registered = mcp_tool.register_mcp_servers({SERVER: _config()})

    assert REGISTRY_TOOL not in registered
    assert registry.get_entry(REGISTRY_TOOL) is None
    assert SERVER not in mcp_tool._servers


def test_lazy_transport_secrets_are_resolved_only_at_connect(monkeypatch):
    from tools import mcp_tool

    secret_ref = "${HERMES_TEST_LAZY_SECRET}"
    monkeypatch.setenv("HERMES_TEST_LAZY_SECRET", "startup-canary")
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "mcp_servers": {
                SERVER: {
                    "url": "https://example.invalid/mcp",
                    "headers": {"Authorization": f"Bearer {secret_ref}"},
                    "lazy_connect": True,
                }
            }
        },
    )
    loaded = mcp_tool._load_mcp_config()[SERVER]
    assert loaded["headers"]["Authorization"] == f"Bearer {secret_ref}"

    monkeypatch.setenv("HERMES_TEST_LAZY_SECRET", "first-call-canary")
    captured = {}

    class _Task:
        def __init__(self, name):
            self.name = name

        async def start(self, config):
            captured.update(config)

    monkeypatch.setattr(mcp_tool, "MCPServerTask", _Task)
    asyncio.run(mcp_tool._connect_server(SERVER, loaded))

    assert captured["headers"]["Authorization"] == "Bearer first-call-canary"


def test_concurrent_first_calls_connect_once_then_use_existing_handler(monkeypatch):
    from tools import mcp_tool
    from tools.registry import registry

    _write_catalog()
    monkeypatch.setattr(mcp_tool, "_MCP_AVAILABLE", True)
    calls = 0
    calls_lock = threading.Lock()
    server = _FakeServer()

    async def _connect(name, config):
        nonlocal calls
        with calls_lock:
            calls += 1
        await asyncio.sleep(0.05)
        return server

    monkeypatch.setattr(mcp_tool, "_connect_server", _connect)
    monkeypatch.setattr(
        mcp_tool,
        "_make_tool_handler",
        lambda server_name, remote_name, timeout: (
            lambda args, **kwargs: json.dumps({"query": args["query"]})
        ),
    )
    mcp_tool.register_mcp_servers({SERVER: _config()})

    results = []

    def _call(index):
        results.append(json.loads(registry.dispatch(REGISTRY_TOOL, {"query": str(index)})))

    threads = [threading.Thread(target=_call, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert all(not thread.is_alive() for thread in threads)
    assert calls == 1
    assert len(results) == 6
    assert SERVER in mcp_tool._servers


def test_schema_drift_fails_closed_and_does_not_publish_server(monkeypatch):
    from tools import mcp_tool
    from tools.registry import registry

    _write_catalog()
    monkeypatch.setattr(mcp_tool, "_MCP_AVAILABLE", True)
    server = _FakeServer(description="Changed live schema")

    async def _connect(name, config):
        return server

    monkeypatch.setattr(mcp_tool, "_connect_server", _connect)
    mcp_tool.register_mcp_servers({SERVER: _config()})

    result = json.loads(registry.dispatch(REGISTRY_TOOL, {"query": "x"}))

    assert result["error_type"] == "mcp_catalog_stale"
    assert "refresh" in result["error"].lower()
    assert SERVER not in mcp_tool._servers
    assert server.shutdown_calls == 1


@pytest.mark.asyncio
async def test_reconnect_discovery_revalidates_static_generation():
    from tools.mcp_tool import MCPServerTask

    catalog = _write_catalog()
    live_tool = _mcp_tool()

    async def _list_tools():
        return SimpleNamespace(tools=[live_tool], nextCursor=None)

    task = MCPServerTask(SERVER)
    task.session = SimpleNamespace(list_tools=_list_tools)
    task.initialize_result = SimpleNamespace(
        capabilities=SimpleNamespace(tools=object(), resources=None, prompts=None)
    )
    task._config = _config()
    task._static_catalog_generation = catalog["generation"]
    task._registered_tool_names = [REGISTRY_TOOL]

    await task._discover_tools()
    assert task._static_catalog_stale is False

    live_tool = _mcp_tool(description="Changed after reconnect")
    with pytest.raises(RuntimeError, match="no longer matches"):
        await task._discover_tools()
    assert task._static_catalog_stale is True


@pytest.mark.asyncio
async def test_reconnect_detects_output_schema_drift():
    from tools import mcp_tool
    from tools.mcp_static_catalog import build_catalog

    initial_tool = _mcp_tool(output_schema={"type": "string"})
    task = mcp_tool.MCPServerTask(SERVER)
    task._config = _config()
    task._tools = [initial_tool]
    task._static_catalog_generation = build_catalog(
        SERVER,
        mcp_tool._catalog_entries_from_server(SERVER, task, _config()),
    )["generation"]
    task.initialize_result = SimpleNamespace(
        capabilities=SimpleNamespace(tools=object(), resources=None, prompts=None)
    )
    task._registered_tool_names = [REGISTRY_TOOL]

    async def _list_tools():
        return SimpleNamespace(
            tools=[_mcp_tool(output_schema={"type": "object"})],
            nextCursor=None,
        )

    task.session = SimpleNamespace(list_tools=_list_tools)

    with pytest.raises(RuntimeError, match="no longer matches"):
        await task._discover_tools()
    assert task._static_catalog_stale is True


def test_failed_first_connect_is_cooled_down(monkeypatch):
    from tools import mcp_tool
    from tools.registry import registry

    _write_catalog()
    monkeypatch.setattr(mcp_tool, "_MCP_AVAILABLE", True)
    monkeypatch.setattr(mcp_tool, "_LAZY_CONNECT_FAILURE_COOLDOWN_S", 60)
    calls = 0

    async def _connect(name, config):
        nonlocal calls
        calls += 1
        raise RuntimeError("offline")

    monkeypatch.setattr(mcp_tool, "_connect_server", _connect)
    mcp_tool.register_mcp_servers({SERVER: _config()})

    first = json.loads(registry.dispatch(REGISTRY_TOOL, {"query": "x"}))
    second = json.loads(registry.dispatch(REGISTRY_TOOL, {"query": "y"}))

    assert calls == 1
    assert first["error_type"] == "mcp_lazy_connect_failed"
    assert second["error_type"] == "mcp_lazy_connect_failed"
    assert second == first

    failed_at, error, generation = mcp_tool._lazy_connect_failures[SERVER]
    mcp_tool._lazy_connect_failures[SERVER] = (
        failed_at - 61, error, generation
    )
    third = json.loads(registry.dispatch(REGISTRY_TOOL, {"query": "z"}))
    assert calls == 2
    assert third["error_type"] == "mcp_lazy_connect_failed"


def test_explicit_snapshot_refresh_connects_writes_and_closes(monkeypatch):
    from tools import mcp_tool
    from tools.mcp_static_catalog import load_catalog

    monkeypatch.setattr(mcp_tool, "_MCP_AVAILABLE", True)
    server = _FakeServer()
    calls = 0

    async def _connect(name, config):
        nonlocal calls
        calls += 1
        return server

    monkeypatch.setattr(mcp_tool, "_connect_server", _connect)
    catalog = mcp_tool.refresh_mcp_static_catalog(SERVER, _config())

    assert calls == 1
    assert server.shutdown_calls == 1
    assert load_catalog(SERVER)["generation"] == catalog["generation"]
    assert SERVER not in mcp_tool._servers
