#!/usr/bin/env python3
"""Executable sink ledger for issue #83565.

Fails when a mapped lower-trust child-process file still contains an ambient
environment construction at a subprocess boundary or when the central policy
module is absent. The ledger is intentionally conservative and auditable.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAPPED = [
    "tools/environments/base.py",
    "tools/environments/local.py",
    "tools/environments/singularity.py",
    "tools/environments/docker.py",
    "agent/lsp/client.py",
    "agent/lsp/install.py",
    "tui_gateway/host_supervisor.py",
    "tui_gateway/methods_tools.py",
    "tui_gateway/server.py",
    "hermes_cli/kanban_db.py",
    "tools/checkpoint_manager.py",
    "tools/code_execution_tool.py",
    "plugins/platforms/whatsapp/adapter.py",
    "agent/secret_sources/bitwarden.py",
    "agent/secret_sources/onepassword.py",
    "hermes_cli/secrets_cli.py",
    "hermes_cli/onepassword_secrets_cli.py",
]
OPTIONAL = [
    "plugins/platforms/raft/adapter.py",
    "plugins/google_meet/meet_bot.py",
    "plugins/google_meet/process_manager.py",
    "plugins/memory/byterover/__init__.py",
    "plugins/platforms/buzz/adapter.py",
    "plugins/platforms/photon/adapter.py",
    "agent/secret_sources/command.py",
]


def ambient(node: ast.AST) -> bool:
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        return (
            node.func.attr == "copy"
            and isinstance(node.func.value, ast.Attribute)
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "os"
            and node.func.value.attr == "environ"
        )
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict" and len(node.args) == 1:
        a = node.args[0]
        return isinstance(a, ast.Attribute) and isinstance(a.value, ast.Name) and a.value.id == "os" and a.attr == "environ"
    return False


def scan(path: Path):
    findings = []
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except Exception as exc:
        return [{"path": str(path.relative_to(ROOT)), "line": None, "kind": "parse-error", "detail": str(exc)}]
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "update" and len(node.args) == 1 and ambient(ast.Call(func=ast.Name(id="dict",ctx=ast.Load()), args=[node.args[0]], keywords=[])):
                findings.append({"path": str(path.relative_to(ROOT)), "line": node.lineno, "kind": "post-scrub-remerge"})
            owner = node.func.value
            if isinstance(owner, ast.Name) and owner.id in {"subprocess", "asyncio"} and node.func.attr in {"Popen", "run", "create_subprocess_exec", "create_subprocess_shell"}:
                env_kw = next((k for k in node.keywords if k.arg == "env"), None)
                if env_kw is None:
                    findings.append({"path": str(path.relative_to(ROOT)), "line": node.lineno, "kind": "spawn-without-env"})
                elif ambient(env_kw.value):
                    findings.append({"path": str(path.relative_to(ROOT)), "line": node.lineno, "kind": "spawn-with-ambient-env"})
    return findings


def main():
    findings = []
    checked = []
    policy = ROOT / "tools/child_process_env_policy.py"
    if not policy.exists():
        findings.append({"path": str(policy.relative_to(ROOT)), "line": None, "kind": "missing-central-policy"})
    for rel in MAPPED + OPTIONAL:
        p = ROOT / rel
        if not p.exists():
            if rel in MAPPED:
                findings.append({"path": rel, "line": None, "kind": "missing-mapped-sink"})
            continue
        checked.append(rel)
        findings.extend(scan(p))
    result = {
        "schema": "hermes.issue-83565.sink-ledger.v2",
        "status": "pass" if not findings else "fail",
        "checked_count": len(checked),
        "checked": checked,
        "findings": findings,
    }
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if not findings else 1)

if __name__ == "__main__":
    main()
