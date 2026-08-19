#!/usr/bin/env python3
"""Assertion-checked first pass for PR #90252.

Replays only the substantive #88875/#90252 file delta from the preserved
pre-rebuild head onto the exact current upstream main.  Three-way application
must either produce an indexed product diff or fail loudly with the exact
unmerged paths; it never chooses an arbitrary conflict side.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ARCHIVE = os.environ["ARCHIVE_BRANCH"]
EXPECTED_BASE = os.environ["EXPECTED_BASE"]

PATHS = [
    ".github/workflows/mcp-dual-era-conformance.yml",
    "agent/transports/hermes_tools_mcp_server.py",
    "hermes_cli/mcp_config.py",
    "hermes_cli/subcommands/mcp.py",
    "hermes_cli/web_routers/mcp.py",
    "mcp_serve.py",
    "tests/e2e/conftest.py",
    "tests/fixtures/mcp_legacy_client.py",
    "tests/fixtures/mcp_legacy_server.py",
    "tests/hermes_cli/test_mcp_dashboard_oauth.py",
    "tests/tools/test_mcp_dashboard_oauth.py",
    "tests/tools/test_mcp_dual_era_wire.py",
    "tests/tools/test_mcp_initial_connect_shutdown.py",
    "tests/tools/test_mcp_input_required.py",
    "tests/tools/test_mcp_list_pagination.py",
    "tests/tools/test_mcp_oauth.py",
    "tests/tools/test_mcp_protocol_negotiation.py",
    "tests/tools/test_mcp_schema_cache.py",
    "tests/tools/test_mcp_schema_cache_ttl.py",
    "tests/tools/test_mcp_tool.py",
    "tests/tui_gateway/test_mcp_oauth_sessions.py",
    "tools/image_generation_tool.py",
    "tools/mcp_dashboard_oauth.py",
    "tools/mcp_oauth.py",
    "tools/mcp_schema_cache.py",
    "tools/mcp_tool.py",
    "tui_gateway/mcp_oauth_sessions.py",
]


def run(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(args), flush=True)
    return subprocess.run(
        args,
        check=check,
        text=True,
        capture_output=capture,
    )


head = run("git", "rev-parse", "HEAD", capture=True).stdout.strip()
if head != EXPECTED_BASE:
    raise SystemExit(f"refusing to repair unexpected base: {head} != {EXPECTED_BASE}")

base = run("git", "merge-base", ARCHIVE, EXPECTED_BASE, capture=True).stdout.strip()
print(f"archive={ARCHIVE} base={base} target={EXPECTED_BASE}")

patch = subprocess.run(
    ["git", "diff", "--binary", f"{base}..{ARCHIVE}", "--", *PATHS],
    check=True,
    stdout=subprocess.PIPE,
).stdout
if not patch:
    raise SystemExit("preserved branch produced no substantive patch")

patch_path = Path("/tmp/pr-90252-preserved.patch")
patch_path.write_bytes(patch)
print(f"patch_bytes={len(patch)}")

applied = subprocess.run(
    ["git", "apply", "--3way", "--index", str(patch_path)],
    text=True,
    capture_output=True,
)
if applied.returncode:
    print(applied.stdout)
    print(applied.stderr, file=sys.stderr)
    run("git", "status", "--short", check=False)
    run("git", "diff", "--name-only", "--diff-filter=U", check=False)
    for path in subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=U"],
        text=True,
        capture_output=True,
        check=False,
    ).stdout.splitlines():
        print(f"\n===== CONFLICT: {path} =====")
        run("git", "diff", "--", path, check=False)
    raise SystemExit(applied.returncode)

run("git", "diff", "--cached", "--check")
changed = run("git", "diff", "--cached", "--name-only", capture=True).stdout.splitlines()
missing = sorted(set(PATHS) - set(changed))
print(f"changed_files={len(changed)}")
print("\n".join(changed))
print(f"preserved_paths_without_delta={missing}")

# The preserved head was evidence, not authority.  This first pass only
# establishes a current-main working tree.  Follow-up control commits refine
# the exact -32602 proof contract, wire matrix, MRTR/listen lifecycle, cache
# identity, and observability before #90252 can leave draft state.
