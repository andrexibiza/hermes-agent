"""Fresh-process E2E coverage for generation-bound lazy MCP catalogs."""

import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest
import yaml


_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_fresh_process_connects_static_mcp_only_on_first_call(tmp_path, monkeypatch):
    pytest.importorskip("mcp.server.fastmcp")

    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    marker = tmp_path / "server-started"
    server_script = tmp_path / "lazy_server.py"
    server_script.write_text(
        textwrap.dedent(
            """
            import os
            from pathlib import Path
            from mcp.server.fastmcp import FastMCP

            Path(os.environ["LAZY_MCP_MARKER"]).write_text(str(os.getpid()), encoding="utf-8")
            mcp = FastMCP("lazy-e2e")

            @mcp.tool()
            def lazy_e2e_echo(value: str) -> str:
                return f"lazy-e2e-ok:{value}"

            if __name__ == "__main__":
                mcp.run(transport="stdio")
            """
        ),
        encoding="utf-8",
    )
    server_config = {
        "enabled": True,
        "lazy_connect": True,
        "command": sys.executable,
        "args": [str(server_script)],
        "env": {"LAZY_MCP_MARKER": str(marker)},
        "tools": {"resources": False, "prompts": False},
    }
    (hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"mcp_servers": {"lazy-e2e": server_config}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    from tools.mcp_tool import refresh_mcp_static_catalog

    catalog = refresh_mcp_static_catalog("lazy-e2e", server_config)
    assert len(catalog["tools"]) == 1
    assert marker.exists()
    marker.unlink()

    child = tmp_path / "fresh_child.py"
    child.write_text(
        textwrap.dedent(
            """
            import json
            import os
            from pathlib import Path
            import sys
            import yaml

            repo_root, home, marker_arg = sys.argv[1:]
            sys.path.insert(0, repo_root)
            os.environ["HERMES_HOME"] = home
            marker = Path(marker_arg)
            config = yaml.safe_load((Path(home) / "config.yaml").read_text())["mcp_servers"]

            from tools import mcp_tool
            from tools.registry import registry
            mcp_tool.register_mcp_servers(config)
            startup_marker = marker.exists()
            startup_connected = sorted(mcp_tool._servers)

            import model_tools
            raw = model_tools.get_tool_definitions(
                enabled_toolsets=["mcp-lazy-e2e"],
                quiet_mode=True,
                skip_tool_search_assembly=True,
            )
            visible = model_tools.get_tool_definitions(
                enabled_toolsets=["mcp-lazy-e2e"],
                quiet_mode=True,
            )
            raw_names = [item["function"]["name"] for item in raw]
            visible_names = [item["function"]["name"] for item in visible]
            result = registry.dispatch(
                "mcp__lazy_e2e__lazy_e2e_echo",
                {"value": "probe"},
            )
            payload = {
                "startup_marker": startup_marker,
                "startup_connected": startup_connected,
                "raw_names": raw_names,
                "visible_names": visible_names,
                "result": result,
                "after_marker": marker.exists(),
                "after_connected": sorted(mcp_tool._servers),
            }
            print(json.dumps(payload))
            mcp_tool.shutdown_mcp_servers()
            """
        ),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    env["PYTHONPATH"] = str(_REPO_ROOT)
    completed = subprocess.run(
        [sys.executable, str(child), str(_REPO_ROOT), str(hermes_home), str(marker)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(completed.stdout.strip().splitlines()[-1])

    assert payload["startup_marker"] is False
    assert payload["startup_connected"] == []
    assert "mcp__lazy_e2e__lazy_e2e_echo" in payload["raw_names"]
    assert "mcp__lazy_e2e__lazy_e2e_echo" not in payload["visible_names"]
    assert {"tool_search", "tool_describe", "tool_call"} <= set(payload["visible_names"])
    assert "lazy-e2e-ok:probe" in payload["result"]
    assert payload["after_marker"] is True
    assert payload["after_connected"] == ["lazy-e2e"]
