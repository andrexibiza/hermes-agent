import json
import subprocess
import sys


def test_issue_83565_sink_ledger_is_clean():
    p = subprocess.run(
        [sys.executable, "scripts/security/verify_child_process_env_boundary.py"],
        text=True,
        capture_output=True,
    )
    assert p.returncode == 0, p.stdout + p.stderr
    data = json.loads(p.stdout)
    assert data["status"] == "pass"
    assert data["findings"] == []
