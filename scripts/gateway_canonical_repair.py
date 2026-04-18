#!/usr/bin/env python3
"""Plan or execute canonical gateway repair for the current HERMES_HOME.

This script is intentionally conservative:
- default mode is dry-run
- --apply is required for side effects
- system-scope repair requires root

The main use case is a default ~/.hermes gateway that is still owned by a
legacy/non-canonical systemd unit such as `hermes-gateway-17b8e69b` while the
canonical unit should be `hermes-gateway`.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterable

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

import hermes_cli.gateway as gateway_cli


def _current_target(requested_scope: str | None = None) -> dict:
    report = gateway_cli.get_gateway_systemd_report(requested_scope=requested_scope)
    if not report.get("installed"):
        raise ValueError("No matching gateway systemd unit found for the current HERMES_HOME")

    system = bool(report.get("system"))
    unit_name = report.get("unit_name") or gateway_cli.get_service_name()
    unit_path = Path(report.get("unit_path")) if report.get("unit_path") else gateway_cli.get_systemd_unit_path(system=system)
    canonical_name = gateway_cli.get_service_name()
    scope = report.get("scope") or ("system" if system else "user")
    configured_user = gateway_cli._read_systemd_user_from_unit(unit_path) if system else None
    outdated = unit_path.exists() and not gateway_cli.systemd_unit_path_is_current(unit_path, system=system)

    return {
        "installed": True,
        "scope": scope,
        "system": system,
        "current_unit": unit_name,
        "current_unit_path": str(unit_path),
        "canonical_unit": canonical_name,
        "drifted": bool(report.get("drifted")),
        "outdated": outdated,
        "active": report.get("active"),
        "configured_user": configured_user,
        "report": report,
    }


def _script_apply_command(requested_scope: str | None = None, cleanup_legacy: bool = False) -> str:
    script_path = Path(__file__).resolve()
    python_path = Path(sys.executable).resolve()
    scope_flag = " --system" if requested_scope == "system" else " --user" if requested_scope == "user" else ""
    suffix = " --cleanup-legacy" if cleanup_legacy else ""
    prefix = "sudo " if requested_scope == "system" else ""
    return f"{prefix}{python_path} {script_path}{scope_flag} --apply{suffix}"


def build_repair_plan(requested_scope: str | None = None, cleanup_legacy: bool = False) -> dict:
    target = _current_target(requested_scope=requested_scope)
    ctl = "sudo systemctl" if target["system"] else "systemctl --user"
    run_as_user = target.get("configured_user") or os.getenv("SUDO_USER") or os.getenv("USER") or "<user>"
    steps: list[dict] = []

    if not target["drifted"] and not target["outdated"]:
        steps.append(
            {
                "id": "noop",
                "summary": "No canonical repair is needed; the current unit already matches the expected service identity and definition.",
                "command": None,
            }
        )
    else:
        if target["outdated"]:
            command = "sudo hermes gateway restart --system" if target["system"] else f"hermes gateway restart{' --user' if target['scope'] == 'user' else ''}"
            steps.append(
                {
                    "id": "refresh-live-unit",
                    "summary": f"Refresh the live unit definition for {target['current_unit']} before or during cutover.",
                    "command": command,
                }
            )

        if target["drifted"]:
            install_cmd = (
                f"sudo hermes gateway install --system --run-as-user {run_as_user}"
                if target["system"]
                else "hermes gateway install --user"
            )
            steps.extend(
                [
                    {
                        "id": "stage-canonical",
                        "summary": f"Stage the canonical unit {target['canonical_unit']} without assuming it owns the live process yet.",
                        "command": install_cmd,
                    },
                    {
                        "id": "disable-legacy",
                        "summary": f"Disable legacy unit {target['current_unit']} so it does not restart during cutover.",
                        "command": f"{ctl} disable {target['current_unit']}",
                    },
                    {
                        "id": "stop-legacy",
                        "summary": f"Stop legacy unit {target['current_unit']}.",
                        "command": f"{ctl} stop {target['current_unit']}",
                    },
                    {
                        "id": "start-canonical",
                        "summary": f"Start canonical unit {target['canonical_unit']}.",
                        "command": f"{ctl} start {target['canonical_unit']}",
                    },
                    {
                        "id": "verify-canonical",
                        "summary": f"Verify that {target['canonical_unit']} is now the active owner.",
                        "command": f"{ctl} status {target['canonical_unit']} --no-pager",
                    },
                    {
                        "id": "rollback-legacy",
                        "summary": f"If canonical start fails, bring legacy unit {target['current_unit']} back immediately.",
                        "command": f"{ctl} start {target['current_unit']}",
                    },
                ]
            )
            if cleanup_legacy:
                steps.append(
                    {
                        "id": "cleanup-legacy-file",
                        "summary": f"Remove the legacy unit file {target['current_unit_path']} after canonical ownership is verified.",
                        "command": f"sudo rm -f {target['current_unit_path']} && {ctl} daemon-reload",
                    }
                )

    return {
        "repair_needed": not (len(steps) == 1 and steps[0]["id"] == "noop"),
        "required_root": bool(target["system"]),
        "current_scope": target["scope"],
        "current_unit": target["current_unit"],
        "current_unit_path": target["current_unit_path"],
        "canonical_unit": target["canonical_unit"],
        "drifted": target["drifted"],
        "outdated": target["outdated"],
        "active": target["active"],
        "configured_user": target.get("configured_user"),
        "cleanup_legacy": cleanup_legacy,
        "steps": steps,
        "apply_command": _script_apply_command(requested_scope=target['scope'], cleanup_legacy=cleanup_legacy),
    }


def _print_plan(plan: dict) -> None:
    print()
    print("Gateway canonical repair plan")
    print("=" * 30)
    print(f"Current unit:   {plan['current_unit']}")
    print(f"Current scope:  {plan['current_scope']}")
    print(f"Canonical unit: {plan['canonical_unit']}")
    print(f"Drifted:        {plan['drifted']}")
    print(f"Outdated:       {plan['outdated']}")
    print(f"Root needed:    {plan['required_root']}")
    if plan.get("configured_user"):
        print(f"Run-as user:    {plan['configured_user']}")
    print()

    for idx, step in enumerate(plan["steps"], start=1):
        print(f"{idx}. {step['summary']}")
        if step.get("command"):
            print(f"   {step['command']}")

    print()
    print("Scripted apply:")
    print(f"  {plan['apply_command']}")
    print()


def apply_repair_plan(requested_scope: str | None = None, cleanup_legacy: bool = False) -> int:
    target = _current_target(requested_scope=requested_scope)
    system = bool(target["system"])
    current_unit = target["current_unit"]
    canonical_unit = target["canonical_unit"]
    unit_path = Path(target["current_unit_path"])
    scope_label = target["scope"]
    run_as_user = target.get("configured_user")
    ctl = gateway_cli._systemctl_cmd(system)

    if system and os.geteuid() != 0:
        raise PermissionError("System-scope canonical repair requires root. Re-run with sudo and --apply.")

    if target["outdated"]:
        changed = gateway_cli.refresh_systemd_unit_path_if_needed(unit_path, system=system)
        if changed:
            print(f"Refreshed unit definition at {unit_path}")

    if target["drifted"]:
        gateway_cli.systemd_install(force=False, system=system, user=not system, run_as_user=run_as_user)
        subprocess.run(ctl + ["disable", current_unit], check=False, timeout=30)
        subprocess.run(ctl + ["stop", current_unit], check=True, timeout=90)
        subprocess.run(ctl + ["start", canonical_unit], check=True, timeout=90)

        verify = gateway_cli.get_gateway_systemd_report(requested_scope=scope_label)
        if verify.get("unit_name") != canonical_unit or verify.get("active") is not True:
            raise RuntimeError(
                f"Canonical unit did not become active (got unit={verify.get('unit_name')} active={verify.get('active')})"
            )
        print(f"Canonical unit {canonical_unit} is now active")

        if cleanup_legacy and unit_path.exists():
            unit_path.unlink()
            subprocess.run(ctl + ["daemon-reload"], check=True, timeout=30)
            print(f"Removed legacy unit file {unit_path}")
    else:
        print("No canonical identity drift detected; nothing to cut over.")

    return 0


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan or execute canonical gateway repair for the current HERMES_HOME")
    parser.add_argument("--apply", action="store_true", help="Execute the repair instead of printing a dry-run plan")
    parser.add_argument("--dry-run", action="store_true", help="Print the repair plan explicitly (this is the default mode)")
    parser.add_argument("--cleanup-legacy", action="store_true", help="Delete the legacy unit file after canonical ownership is verified")
    scope_group = parser.add_mutually_exclusive_group()
    scope_group.add_argument("--system", action="store_true", help="Force system-scope planning/execution")
    scope_group.add_argument("--user", action="store_true", help="Force user-scope planning/execution")
    args = parser.parse_args(list(argv) if argv is not None else None)
    requested_scope = "system" if args.system else "user" if args.user else None

    if args.apply:
        return apply_repair_plan(requested_scope=requested_scope, cleanup_legacy=args.cleanup_legacy)

    plan = build_repair_plan(requested_scope=requested_scope, cleanup_legacy=args.cleanup_legacy)
    _print_plan(plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
