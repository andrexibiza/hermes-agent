from pathlib import Path

from scripts.gateway_canonical_repair import build_repair_plan
import scripts.gateway_canonical_repair as repair_script


def test_build_repair_plan_for_legacy_system_unit(tmp_path, monkeypatch):
    unit_path = tmp_path / "hermes-gateway-17b8e69b.service"
    unit_path.write_text("[Unit]\n", encoding="utf-8")

    monkeypatch.setattr(
        repair_script.gateway_cli,
        "get_gateway_systemd_report",
        lambda requested_scope=None: {
            "installed": True,
            "system": True,
            "scope": "system",
            "unit_name": "hermes-gateway-17b8e69b",
            "unit_path": str(unit_path),
            "drifted": True,
            "active": True,
        },
    )
    monkeypatch.setattr(repair_script.gateway_cli, "get_service_name", lambda: "hermes-gateway")
    monkeypatch.setattr(repair_script.gateway_cli, "systemd_unit_path_is_current", lambda path, system=False: False)
    monkeypatch.setattr(repair_script.gateway_cli, "_read_systemd_user_from_unit", lambda path: "wutj")

    plan = build_repair_plan(cleanup_legacy=True)

    commands = [step["command"] for step in plan["steps"] if step.get("command")]
    assert plan["repair_needed"] is True
    assert plan["required_root"] is True
    assert plan["current_unit"] == "hermes-gateway-17b8e69b"
    assert plan["canonical_unit"] == "hermes-gateway"
    assert "sudo hermes gateway restart --system" in commands
    assert "sudo hermes gateway install --system --run-as-user wutj" in commands
    assert "sudo systemctl disable hermes-gateway-17b8e69b" in commands
    assert "sudo systemctl stop hermes-gateway-17b8e69b" in commands
    assert "sudo systemctl start hermes-gateway" in commands
    assert any("rm -f" in command for command in commands)
    assert plan["apply_command"].endswith("--apply --cleanup-legacy")


def test_build_repair_plan_is_noop_when_canonical_unit_is_current(tmp_path, monkeypatch):
    unit_path = tmp_path / "hermes-gateway.service"
    unit_path.write_text("[Unit]\n", encoding="utf-8")

    monkeypatch.setattr(
        repair_script.gateway_cli,
        "get_gateway_systemd_report",
        lambda requested_scope=None: {
            "installed": True,
            "system": True,
            "scope": "system",
            "unit_name": "hermes-gateway",
            "unit_path": str(unit_path),
            "drifted": False,
            "active": True,
        },
    )
    monkeypatch.setattr(repair_script.gateway_cli, "get_service_name", lambda: "hermes-gateway")
    monkeypatch.setattr(repair_script.gateway_cli, "systemd_unit_path_is_current", lambda path, system=False: True)
    monkeypatch.setattr(repair_script.gateway_cli, "_read_systemd_user_from_unit", lambda path: "wutj")

    plan = build_repair_plan()

    assert plan["repair_needed"] is False
    assert plan["steps"] == [
        {
            "id": "noop",
            "summary": "No canonical repair is needed; the current unit already matches the expected service identity and definition.",
            "command": None,
        }
    ]


def test_build_repair_plan_for_user_scope_avoids_sudo(tmp_path, monkeypatch):
    unit_path = tmp_path / "hermes-gateway-coder.service"
    unit_path.write_text("[Unit]\n", encoding="utf-8")

    monkeypatch.setattr(
        repair_script.gateway_cli,
        "get_gateway_systemd_report",
        lambda requested_scope=None: {
            "installed": True,
            "system": False,
            "scope": "user",
            "unit_name": "hermes-gateway-deadbeef",
            "unit_path": str(unit_path),
            "drifted": True,
            "active": True,
        },
    )
    monkeypatch.setattr(repair_script.gateway_cli, "get_service_name", lambda: "hermes-gateway")
    monkeypatch.setattr(repair_script.gateway_cli, "systemd_unit_path_is_current", lambda path, system=False: True)
    monkeypatch.setattr(repair_script.gateway_cli, "_read_systemd_user_from_unit", lambda path: None)

    plan = build_repair_plan()

    commands = [step["command"] for step in plan["steps"] if step.get("command")]
    assert plan["required_root"] is False
    assert "hermes gateway install --user" in commands
    assert "systemctl --user disable hermes-gateway-deadbeef" in commands
    assert "systemctl --user start hermes-gateway" in commands
    assert " --user --apply" in plan["apply_command"]
    assert all(not command.startswith("sudo ") for command in commands)
