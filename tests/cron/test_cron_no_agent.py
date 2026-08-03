"""Tests for cronjob no_agent mode — script-driven jobs that skip the LLM.

Covers:

* ``create_job(no_agent=True)`` shape, validation, and serialization.
* ``cronjob(action='create', no_agent=True)`` tool-level validation.
* ``cronjob(action='update')`` flipping no_agent on/off.
* ``scheduler.run_job`` short-circuit path: success/silent/failure.
* Shell script support in ``_run_job_script`` (.sh runs via bash).
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    """Isolate HERMES_HOME for each test so jobs/scripts don't leak."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()

    monkeypatch.setenv("HERMES_HOME", str(home))

    # Reload modules that cache get_hermes_home() at import time.
    import importlib
    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.jobs
    importlib.reload(cron.jobs)
    import cron.scheduler
    importlib.reload(cron.scheduler)

    return home


# ---------------------------------------------------------------------------
# create_job / update_job: data-layer semantics
# ---------------------------------------------------------------------------


def test_create_job_no_agent_requires_script(hermes_env):
    from cron.jobs import create_job

    with pytest.raises(ValueError, match="no_agent=True requires a script"):
        create_job(prompt=None, schedule="every 5m", no_agent=True)


def test_update_job_roundtrips_no_agent_flag(hermes_env):
    from cron.jobs import create_job, update_job, get_job

    script_path = hermes_env / "scripts" / "w.sh"
    script_path.write_text("echo hi\n")
    job = create_job(prompt=None, schedule="every 5m", script="w.sh", no_agent=True, deliver="local")

    update_job(job["id"], {"no_agent": False})
    reloaded = get_job(job["id"])
    assert reloaded["no_agent"] is False

    update_job(job["id"], {"no_agent": True})
    reloaded = get_job(job["id"])
    assert reloaded["no_agent"] is True


# ---------------------------------------------------------------------------
# cronjob tool: API-layer validation
# ---------------------------------------------------------------------------


def test_cronjob_tool_create_no_agent_without_script_errors(hermes_env):
    from tools.cronjob_tools import cronjob

    result = json.loads(
        cronjob(action="create", schedule="every 5m", no_agent=True, deliver="local")
    )
    assert result.get("success") is False
    assert "no_agent=True requires a script" in result.get("error", "")


# ---------------------------------------------------------------------------
# scheduler.run_job: short-circuit behavior
# ---------------------------------------------------------------------------


def test_run_job_no_agent_success_returns_script_stdout(hermes_env):
    """Happy path: script exits 0 with output, delivered verbatim."""
    from cron.jobs import create_job
    from cron.scheduler import run_job

    script_path = hermes_env / "scripts" / "alert.sh"
    script_path.write_text("#!/bin/bash\necho 'RAM 92% on host'\n")

    job = create_job(
        prompt=None, schedule="every 5m", script="alert.sh", no_agent=True, deliver="local"
    )
    success, doc, final_response, error = run_job(job)
    assert success is True
    assert error is None
    assert "RAM 92% on host" in final_response
    assert "RAM 92% on host" in doc


# ---------------------------------------------------------------------------
# _run_job_script: shell-script support
# ---------------------------------------------------------------------------


def test_run_job_script_path_traversal_still_blocked(hermes_env):
    """Security regression: shell-script support must NOT loosen containment."""
    from cron.scheduler import _run_job_script

    # Absolute path outside the scripts dir should be rejected.
    ok, output = _run_job_script("/etc/passwd")
    assert ok is False
    assert "Blocked" in output or "outside" in output


# ---------------------------------------------------------------------------
# _bash_script_arg: Windows MSYS path conversion (#23404 / #65317)
# ---------------------------------------------------------------------------


def test_bash_script_arg_converts_windows_drive_path_to_msys(monkeypatch):
    """A native C:\\... script path must become /c/... for git-bash.

    Regression for the exit-127 failure: git-bash treats '\\' as an escape
    char and collapses ``C:\\Users\\...\\x.sh`` to ``C:Users...x.sh`` so the
    script is never found.  The MSYS form (``/c/Users/.../x.sh``) is what
    git-bash accepts natively.
    """
    from pathlib import PureWindowsPath

    import cron.scheduler as scheduler

    monkeypatch.setattr(scheduler.sys, "platform", "win32")
    win_path = PureWindowsPath(r"C:\Users\denis\.hermes\scripts\hermes-backup.sh")
    assert scheduler._bash_script_arg(win_path) == (
        "/c/Users/denis/.hermes/scripts/hermes-backup.sh"
    )


def test_bash_script_arg_leaves_posix_path_unchanged(monkeypatch):
    """POSIX: the resolved script path is passed to bash verbatim."""
    from pathlib import PurePosixPath

    import cron.scheduler as scheduler

    monkeypatch.setattr(scheduler.sys, "platform", "linux")
    posix_path = PurePosixPath("/home/denis/.hermes/scripts/hermes-backup.sh")
    assert scheduler._bash_script_arg(posix_path) == (
        "/home/denis/.hermes/scripts/hermes-backup.sh"
    )


def test_run_job_script_windows_argv_uses_msys_path(hermes_env, monkeypatch):
    """End-to-end: on Windows, bash receives the /c/... script argument.

    Capture subprocess.run and assert the argv handed to git-bash carries
    the MSYS form with no backslashes (the exact argv that previously
    produced exit 127).
    """
    import subprocess as _subprocess

    import cron.scheduler as scheduler
    from cron.scheduler import _run_job_script

    script_path = hermes_env / "scripts" / "nightly.sh"
    script_path.write_text('printf "ok\\n"\n', encoding="utf-8")

    captured = {}

    def fake_run(argv, *args, **kwargs):
        captured["argv"] = list(argv)
        return _subprocess.CompletedProcess(argv, 0, "ok\n", "")

    monkeypatch.setattr(_subprocess, "run", fake_run)
    # Force the Windows branch regardless of the host platform, and provide
    # a bash so the test does not depend on a local git-bash install.
    monkeypatch.setattr(scheduler.sys, "platform", "win32")
    monkeypatch.setattr(
        scheduler.shutil, "which", lambda name: "/usr/bin/bash"
    )

    ok, output = _run_job_script("nightly.sh")
    assert ok is True
    argv = captured["argv"]
    assert len(argv) == 2
    script_arg = argv[1]
    assert script_arg.startswith("/c/")
    assert "\\" not in script_arg


def test_run_job_script_posix_argv_unchanged(hermes_env, monkeypatch):
    """POSIX: the script path is passed to the interpreter untouched."""
    import subprocess as _subprocess

    import cron.scheduler as scheduler
    from cron.scheduler import _run_job_script

    script_path = hermes_env / "scripts" / "nightly.sh"
    script_path.write_text('printf "ok\\n"\n', encoding="utf-8")

    captured = {}

    def fake_run(argv, *args, **kwargs):
        captured["argv"] = list(argv)
        return _subprocess.CompletedProcess(argv, 0, "ok\n", "")

    monkeypatch.setattr(_subprocess, "run", fake_run)
    monkeypatch.setattr(scheduler.sys, "platform", "linux")
    monkeypatch.setattr(
        scheduler.shutil, "which", lambda name: "/usr/bin/bash"
    )

    ok, output = _run_job_script("nightly.sh")
    assert ok is True
    argv = captured["argv"]
    assert len(argv) == 2
    # The Windows MSYS conversion (C:\... -> /c/...) must NOT have run.
    assert not argv[1].startswith("/c/")
    expected = str((hermes_env / "scripts" / "nightly.sh").resolve())
    assert argv[1] == expected
