from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import hermes_cli.image_provenance as image_provenance
import hermes_cli.update_receipt as receipts
from hermes_cli.update_contract import IMAGE_MANAGED_UPDATE_REFUSED


def _write_marker(path):
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "deployment_kind": "image",
                "manager": "docker",
                "image": "nousresearch/hermes-agent",
                "version": "0.20.5",
                "revision": "f" * 40,
            }
        ),
        encoding="utf-8",
    )
    return path


def _boom(name):
    def fail(*_args, **_kwargs):
        raise AssertionError(f"{name} must not run before image refusal")

    return fail


def test_cli_refuses_before_io_lock_backup_or_subprocess(monkeypatch, tmp_path, capsys):
    import hermes_cli.config as config
    import hermes_cli.main as main
    from hermes_cli.update_lock import UpdateLock

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    marker = _write_marker(tmp_path / "image.json")
    monkeypatch.setattr(image_provenance, "IMAGE_PROVENANCE_PATH", marker)
    monkeypatch.setattr(main, "PROJECT_ROOT", checkout)
    monkeypatch.setattr(config, "is_managed", lambda: False)
    # Refusal must happen before any of the current command-boundary side
    # effects.  These are the actual seams owned by cmd_update today; do not
    # pin this witness to retired helper names from older updater shapes.
    monkeypatch.setattr(main, "_install_hangup_protection", _boom("I/O setup"))
    monkeypatch.setattr(UpdateLock, "acquire", _boom("update lock"))
    monkeypatch.setattr(main, "_run_pre_update_backup", _boom("backup"))
    monkeypatch.setattr("subprocess.run", _boom("subprocess"))
    monkeypatch.setattr(receipts, "_receipt_dir", lambda: tmp_path / "receipts")
    receipts._current = None

    with pytest.raises(SystemExit) as exc:
        main.cmd_update(SimpleNamespace(plan=False, check=False, branch=None))

    assert exc.value.code == 2
    assert "image-managed" in capsys.readouterr().out
    payload = json.loads(
        (tmp_path / "receipts" / "latest.json").read_text(encoding="utf-8")
    )
    assert payload["outcome"] == "refused"
    assert payload["refusal"]["code"] == IMAGE_MANAGED_UPDATE_REFUSED


def test_direct_update_impl_refuses_before_mutation(monkeypatch, tmp_path):
    import hermes_cli.main as main
    from hermes_cli.update_cmd import _cmd_update_impl

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    marker = _write_marker(tmp_path / "image.json")
    monkeypatch.setattr(image_provenance, "IMAGE_PROVENANCE_PATH", marker)
    monkeypatch.setattr(main, "PROJECT_ROOT", checkout)
    monkeypatch.setattr(main, "_run_pre_update_backup", _boom("backup"))
    monkeypatch.setattr("subprocess.run", _boom("subprocess"))
    monkeypatch.setattr(receipts, "_receipt_dir", lambda: tmp_path / "receipts")
    receipts._current = None

    with pytest.raises(SystemExit) as exc:
        _cmd_update_impl(SimpleNamespace(branch=None), gateway_mode=False)

    assert exc.value.code == 2


def test_dashboard_api_returns_shared_refusal_without_spawn(monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    import hermes_cli.web_server as web_server

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    marker = _write_marker(tmp_path / "image.json")
    monkeypatch.setattr(image_provenance, "IMAGE_PROVENANCE_PATH", marker)
    monkeypatch.setattr(web_server, "PROJECT_ROOT", checkout)
    monkeypatch.setattr(
        web_server,
        "_dashboard_local_update_managed_externally",
        _boom("legacy dashboard guard"),
    )
    monkeypatch.setattr(web_server, "_spawn_hermes_action", _boom("update spawn"))
    monkeypatch.setattr(receipts, "_receipt_dir", lambda: tmp_path / "receipts")
    receipts._current = None

    result = asyncio.run(web_server.update_hermes())

    assert result["ok"] is False
    assert result["error"] == IMAGE_MANAGED_UPDATE_REFUSED
    assert result["reason"] == IMAGE_MANAGED_UPDATE_REFUSED
    assert result["deployment_kind"] == "image"
    assert result["update_command"].startswith("docker pull")
    assert result["receipt_path"]
