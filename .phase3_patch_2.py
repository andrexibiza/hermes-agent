#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one literal match, found {count}")
    write(path, text.replace(old, new, 1))


def sub_once(path: str, pattern: str, replacement: str) -> None:
    text = read(path)
    new, count = re.subn(pattern, replacement, text, count=1, flags=re.DOTALL)
    if count != 1:
        raise RuntimeError(f"{path}: expected one regex match, found {count}")
    write(path, new)


MAIN_REPLACEMENT = '    from hermes_cli.update_contract import UPDATE_REFUSED_EXIT, perform_update\n\n    refusal = perform_update(\n        surface="cli",\n        requested_target=getattr(args, "branch", None),\n        project_root=PROJECT_ROOT,\n    )\n    if refusal is not None:\n        print(refusal.message)\n        sys.exit(UPDATE_REFUSED_EXIT)\n\n    install_method = detect_install_method(PROJECT_ROOT)\n    if is_nix_install_method(install_method) or install_method == "apt":'
WEB_REMOVE_REPLACEMENT = '    install_method = detect_install_method(PROJECT_ROOT)\n    if is_nix_install_method(install_method) or install_method == "apt":'

sub_once(
    "hermes_cli/main.py",
    r'    # Docker users can\'t ``git pull``.*?'
    r'    install_method = detect_install_method\(PROJECT_ROOT\)\n'
    r'    if install_method == "docker":\n.*?'
    r'    if is_nix_install_method\(install_method\) or install_method == "apt":',
    MAIN_REPLACEMENT,
)

replace_once(
    "hermes_cli/update_cmd.py",
    'def _cmd_update_impl(args, gateway_mode: bool):\n'
    '    """Body of ``cmd_update`` — kept separate so the wrapper can always\n'
    '    restore stdio even on ``sys.exit``."""\n',
    'def _cmd_update_impl(args, gateway_mode: bool):\n'
    '    """Body of ``cmd_update`` — kept separate so the wrapper can always\n'
    '    restore stdio even on ``sys.exit``."""\n'
    '    from hermes_cli.update_contract import UPDATE_REFUSED_EXIT, perform_update\n\n'
    '    refusal = perform_update(\n'
    '        surface="gateway" if gateway_mode else "internal",\n'
    '        requested_target=getattr(args, "branch", None),\n'
    '        project_root=_m().PROJECT_ROOT,\n'
    '    )\n'
    '    if refusal is not None:\n'
    '        print(refusal.message)\n'
    '        sys.exit(UPDATE_REFUSED_EXIT)\n\n',
)

replace_once(
    "hermes_cli/web_server.py",
    '@app.post("/api/hermes/update")\n'
    'async def update_hermes():\n'
    '    """Kick off ``hermes update`` in the background."""\n'
    '    if _dashboard_local_update_managed_externally():\n',
    '@app.post("/api/hermes/update")\n'
    'async def update_hermes():\n'
    '    """Kick off ``hermes update`` in the background."""\n'
    '    from hermes_cli.update_contract import UPDATE_REFUSED_EXIT, perform_update\n\n'
    '    refusal = perform_update(\n'
    '        surface="dashboard_api",\n'
    '        project_root=PROJECT_ROOT,\n'
    '    )\n'
    '    if refusal is not None:\n'
    '        _record_completed_action(\n'
    '            "hermes-update", refusal.message, exit_code=UPDATE_REFUSED_EXIT\n'
    '        )\n'
    '        return {\n'
    '            "ok": False,\n'
    '            "pid": None,\n'
    '            "name": "hermes-update",\n'
    '            "error": refusal.code,\n'
    '            "reason": refusal.code,\n'
    '            "message": refusal.message,\n'
    '            "update_command": refusal.update_command,\n'
    '            "deployment_kind": refusal.deployment_kind,\n'
    '            "baked_identity": refusal.baked_identity,\n'
    '            "current_identity": refusal.current_identity,\n'
    '            "receipt_path": refusal.receipt_path,\n'
    '        }\n\n'
    '    if _dashboard_local_update_managed_externally():\n',
)
sub_once(
    "hermes_cli/web_server.py",
    r'    install_method = detect_install_method\(PROJECT_ROOT\)\n'
    r'    if install_method == "docker":\n.*?'
    r'    if is_nix_install_method\(install_method\) or install_method == "apt":',
    WEB_REMOVE_REPLACEMENT,
)

replace_once(
    "Dockerfile",
    'ARG HERMES_GIT_SHA=\n'
    'RUN if [ -n "${HERMES_GIT_SHA}" ]; then \\\n'
    '        printf \'%s\\n\' "${HERMES_GIT_SHA}" > /opt/hermes/.hermes_build_sha; \\\n'
    '    fi\n',
    'ARG HERMES_GIT_SHA=\n'
    '# Image provenance lives outside both the checkout and HERMES_HOME. A\n'
    '# bind-mounted repository (including .git) cannot hide this build fact.\n'
    'RUN mkdir -p /etc/hermes && \\\n'
    '    HERMES_GIT_SHA="${HERMES_GIT_SHA}" python3 -c \'import json, os, pathlib, tomllib; root = pathlib.Path("/opt/hermes"); version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]; payload = {"schema": 1, "deployment_kind": "image", "manager": "docker", "image": "nousresearch/hermes-agent", "version": str(version), "revision": os.environ.get("HERMES_GIT_SHA") or None}; pathlib.Path("/etc/hermes/image-provenance.json").write_text(json.dumps(payload, sort_keys=True) + "\\n", encoding="utf-8")\' && \\\n'
    '    chmod 0444 /etc/hermes/image-provenance.json && \\\n'
    '    if [ -n "${HERMES_GIT_SHA}" ]; then \\\n'
    '        printf \'%s\\n\' "${HERMES_GIT_SHA}" > /opt/hermes/.hermes_build_sha; \\\n'
    '    fi\n',
)


print("phase3 entrypoint/image patch complete")
