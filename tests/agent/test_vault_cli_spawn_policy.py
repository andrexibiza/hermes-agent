from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from agent.secret_sources import bitwarden, onepassword
from hermes_cli import onepassword_secrets_cli, secrets_cli


_FORBIDDEN = {
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_RUN_ID",
    "_HERMES_GATEWAY",
    "NODE_OPTIONS",
    "PYTHONPATH",
}


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)


def _assert_strict_env(env: dict[str, str]) -> None:
    assert not (_FORBIDDEN & set(env))
    assert env["NO_COLOR"] == "1"


def test_bitwarden_fetch_receives_only_baseline_and_explicit_grant(monkeypatch):
    source = {
        "PATH": "/usr/bin",
        "HOME": "/home/user",
        "BWS_SERVER_URL": "https://vault.example",
        "OPENAI_API_KEY": "provider-secret",
        "HERMES_KANBAN_TASK": "parent-task",
        "_HERMES_GATEWAY": "1",
        "NODE_OPTIONS": "--require attacker.js",
    }
    captured = {}

    monkeypatch.setattr(bitwarden, "get_source_environment", lambda: source)

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured.update(kwargs)
        return _completed(stdout="[]")

    monkeypatch.setattr(bitwarden.subprocess, "run", fake_run)
    secrets, warnings = bitwarden._run_bws_list(
        Path("/usr/bin/bws"), "edge-token", "project-id"
    )

    assert secrets == {}
    assert warnings == []
    assert captured["stdin"] is subprocess.DEVNULL
    env = captured["env"]
    _assert_strict_env(env)
    assert env["PATH"] == "/usr/bin"
    assert env["HOME"] == "/home/user"
    assert env["BWS_ACCESS_TOKEN"] == "edge-token"
    assert env["BWS_SERVER_URL"] == "https://vault.example"


def test_onepassword_read_receives_only_declared_auth_capability(monkeypatch):
    source = {
        "PATH": "/usr/bin",
        "HOME": "/home/user",
        "OP_ACCOUNT": "team",
        "OP_SESSION_team": "session-secret",
        "OP_LOAD_DESKTOP_APP_SETTINGS": "false",
        "OP_CACHE": "false",
        "OPENAI_API_KEY": "provider-secret",
        "HERMES_KANBAN_RUN_ID": "parent-run",
        "PYTHONPATH": "/attacker",
    }
    captured = {}

    monkeypatch.setattr(onepassword, "get_source_environment", lambda: source)

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured.update(kwargs)
        return _completed(stdout="resolved-value\n")

    monkeypatch.setattr(onepassword.subprocess, "run", fake_run)
    value = onepassword._run_op_read(
        Path("/usr/bin/op"),
        "op://Private/item/field",
        account="team",
        token_value="service-token",
    )

    assert value == "resolved-value"
    assert captured["stdin"] is subprocess.DEVNULL
    env = captured["env"]
    _assert_strict_env(env)
    assert env["OP_ACCOUNT"] == "team"
    assert env["OP_SESSION_team"] == "session-secret"
    assert env["OP_LOAD_DESKTOP_APP_SETTINGS"] == "false"
    assert env["OP_CACHE"] == "false"
    assert env["OP_SERVICE_ACCOUNT_TOKEN"] == "service-token"


def test_bws_version_probe_is_credential_free_and_stdin_closed(monkeypatch):
    captured = {}
    for key in _FORBIDDEN | {"BWS_ACCESS_TOKEN"}:
        monkeypatch.setenv(key, "ambient-secret")

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return _completed(stdout="bws 2.0.0\n")

    monkeypatch.setattr(secrets_cli.subprocess, "run", fake_run)
    assert secrets_cli._bws_version(Path("/usr/bin/bws")) == "bws 2.0.0"
    assert captured["stdin"] is subprocess.DEVNULL
    env = captured["env"]
    _assert_strict_env(env)
    assert "BWS_ACCESS_TOKEN" not in env


def test_bws_project_probe_uses_exact_candidate_token(monkeypatch):
    captured = {}
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "ambient-token")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return _completed(stdout="[]")

    monkeypatch.setattr(secrets_cli.subprocess, "run", fake_run)
    projects = secrets_cli._list_projects(
        Path("/usr/bin/bws"), "candidate-token", secrets_cli.Console()
    )
    assert projects == []
    assert captured["stdin"] is subprocess.DEVNULL
    env = captured["env"]
    _assert_strict_env(env)
    assert env["BWS_ACCESS_TOKEN"] == "candidate-token"


def test_op_version_probe_is_credential_free_and_stdin_closed(monkeypatch):
    captured = {}
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "ambient-token")
    monkeypatch.setenv("OP_SESSION_team", "ambient-session")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return _completed(stdout="2.30.0\n")

    monkeypatch.setattr(onepassword_secrets_cli.subprocess, "run", fake_run)
    assert onepassword_secrets_cli._op_version(Path("/usr/bin/op")) == "2.30.0"
    assert captured["stdin"] is subprocess.DEVNULL
    env = captured["env"]
    _assert_strict_env(env)
    assert "OP_SERVICE_ACCOUNT_TOKEN" not in env
    assert "OP_SESSION_team" not in env


def test_op_whoami_uses_candidate_token_without_other_hermes_authority(monkeypatch):
    captured = {}
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "ambient-token")
    monkeypatch.setenv("OP_SESSION_team", "session-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "provider-secret")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "parent-task")

    def fake_run(cmd, **kwargs):
        captured.update(kwargs)
        return _completed(stdout="team@example.com\n")

    monkeypatch.setattr(onepassword_secrets_cli.subprocess, "run", fake_run)
    assert (
        onepassword_secrets_cli._op_whoami(
            Path("/usr/bin/op"), "team", token_value="candidate-token"
        )
        == "team@example.com"
    )
    assert captured["stdin"] is subprocess.DEVNULL
    env = captured["env"]
    _assert_strict_env(env)
    assert env["OP_SERVICE_ACCOUNT_TOKEN"] == "candidate-token"
    assert env["OP_SESSION_team"] == "session-secret"
    assert env.get("OP_SERVICE_ACCOUNT_TOKEN") != os.environ["OP_SERVICE_ACCOUNT_TOKEN"]
