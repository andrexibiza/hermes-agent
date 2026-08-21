from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli.deployment_plan import (
    DeploymentKind,
    DeploymentMode,
    DeploymentMutationDenied,
    DeploymentPlanInvalid,
    admit_update,
    load_deployment_plan,
)


def _write_plan(home: Path, **overrides) -> Path:
    payload = {
        "schema_version": 1,
        "mode": "cli-only",
        "kind": "git-venv",
        "canonical_checkout": str(home / "repo"),
    }
    payload.update(overrides)
    path = home / "deployment-plan.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_missing_plan_uses_compatible_cli_only_authority(tmp_path):
    repo = tmp_path / "repo"
    plan = load_deployment_plan(
        env={"HERMES_HOME": str(tmp_path / "home")},
        project_root=repo,
    )
    assert plan.mode is DeploymentMode.CLI_ONLY
    assert plan.kind is DeploymentKind.GIT_VENV
    assert plan.canonical_checkout == repo.resolve()
    assert "desktop" not in plan.allowed_components


def test_explicit_but_corrupt_plan_fails_closed(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "deployment-plan.json").write_text("{", encoding="utf-8")
    with pytest.raises(DeploymentPlanInvalid, match="cannot read deployment plan"):
        load_deployment_plan(
            env={"HERMES_HOME": str(home)},
            project_root=tmp_path / "repo",
        )


def test_remote_only_cannot_smuggle_local_components(tmp_path):
    home = tmp_path / "home"
    _write_plan(
        home,
        mode="remote-only",
        kind="externally-managed",
        canonical_checkout=None,
        allowed_components=["desktop-client", "python-runtime"],
    )
    with pytest.raises(DeploymentPlanInvalid, match="cannot authorize components"):
        load_deployment_plan(
            env={"HERMES_HOME": str(home)},
            project_root=tmp_path / "repo",
        )


@pytest.mark.parametrize(
    ("mode", "kind", "message"),
    [
        ("remote-only", "externally-managed", "not updatable in place"),
        ("cli-only", "image-managed", "not updatable in place"),
    ],
)
def test_non_owned_deployments_refuse_in_place_update(tmp_path, mode, kind, message):
    home = tmp_path / "home"
    _write_plan(
        home,
        mode=mode,
        kind=kind,
        canonical_checkout=None,
        automatic_local_provisioning=False,
    )
    args = argparse.Namespace(check=False)
    with pytest.raises(DeploymentMutationDenied, match=message):
        admit_update(
            args,
            env={"HERMES_HOME": str(home)},
            project_root=tmp_path / "repo",
        )


def test_update_check_is_observation_for_image_managed_install(tmp_path):
    home = tmp_path / "home"
    _write_plan(
        home,
        kind="image-managed",
        canonical_checkout=None,
        automatic_local_provisioning=False,
    )
    args = argparse.Namespace(check=True)
    plan = admit_update(
        args,
        env={"HERMES_HOME": str(home)},
        project_root=tmp_path / "repo",
    )
    assert plan.kind is DeploymentKind.IMAGE_MANAGED
    assert args._deployment_plan is plan


def test_mutation_requires_the_canonical_checkout(tmp_path):
    home = tmp_path / "home"
    _write_plan(home)
    with pytest.raises(DeploymentMutationDenied, match="canonical checkout"):
        admit_update(
            argparse.Namespace(check=False),
            env={"HERMES_HOME": str(home)},
            project_root=tmp_path / "other",
        )


def test_admitted_update_carries_plan_and_digest(tmp_path):
    home = tmp_path / "home"
    repo = home / "repo"
    _write_plan(home)
    env = {"HERMES_HOME": str(home)}
    args = argparse.Namespace(check=False)
    plan = admit_update(args, env=env, project_root=repo)
    assert args._deployment_plan is plan
    assert env["HERMES_DEPLOYMENT_PLAN_DIGEST"] == plan.digest
    assert len(plan.digest) == 64


def test_remote_only_never_authorizes_local_provisioning(tmp_path):
    home = tmp_path / "home"
    _write_plan(
        home,
        mode="remote-only",
        kind="externally-managed",
        canonical_checkout=None,
        automatic_local_provisioning=True,
    )
    with pytest.raises(DeploymentPlanInvalid, match="remote-only"):
        load_deployment_plan(
            env={"HERMES_HOME": str(home)},
            project_root=tmp_path / "repo",
        )
