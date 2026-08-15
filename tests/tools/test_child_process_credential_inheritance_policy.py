import json
import os
import subprocess
import sys

from tools.child_process_env_policy import filter_child_env


def test_real_child_cannot_observe_planted_credentials(monkeypatch):
    planted = {
        "OPENAI_API_KEY": "provider",
        "GH_TOKEN": "github",
        "DB_PASS": "db-pass",
        "APPTAINERENV_BWS_ACCESS_TOKEN": "wrapped-vault",
        "DATABASE_URL": "postgresql://u:p@db/app",
        "HERMES_TEST_SAFE": "visible",
    }
    for key, value in planted.items():
        monkeypatch.setenv(key, value)
    env = filter_child_env(os.environ, provider_blocklist=["OPENAI_API_KEY"])
    code = "import json,os;print(json.dumps(dict(os.environ)))"
    p = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True)
    child = json.loads(p.stdout)
    for key in planted:
        if key != "HERMES_TEST_SAFE":
            assert key not in child
    assert child["HERMES_TEST_SAFE"] == "visible"


def test_provider_inheritance_is_narrow(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "provider")
    monkeypatch.setenv("GH_TOKEN", "github")
    env = filter_child_env(
        os.environ,
        provider_blocklist=["OPENAI_API_KEY"],
        inherit_provider_credentials=True,
    )
    assert env["OPENAI_API_KEY"] == "provider"
    assert "GH_TOKEN" not in env
