from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import deployment_plan
from hermes_cli.subcommands.update import build_update_parser
from hermes_cli.update_deployment_guard import (
    enforce_legacy_update_envelope,
    validate_update_plan_source,
)


def _parser(cmd_update):
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    build_update_parser(subparsers, cmd_update=cmd_update)
    return parser


def _mutable_plan(tmp_path: Path, **overrides):
    repo = tmp_path / "repo"
    payload = {
        "schema_version": 1,
        "mode": "cli-only",
        "kind": "git-venv",
        "canonical_checkout": str(repo),
        "automatic_local_provisioning": True,
    }
    payload.update(overrides)
    return deployment_plan.plan_from_mapping(
        payload,
        hermes_home=tmp_path / "home",
        source="test",
        project_root=repo,
    )


def test_update_handler_admits_before_calling_implementation(monkeypatch):
    calls = []

    def fake_admit(args):
        calls.append(("plan", args.check))

    def fake_update(args):
        calls.append(("update", args.check))
        return 17

    monkeypatch.delenv("HERMES_DEPLOYMENT_PLAN", raising=False)
    monkeypatch.setattr(deployment_plan, "admit_update", fake_admit)
    args = _parser(fake_update).parse_args(["update", "--check"])
    assert args.func(args) == 17
    assert calls == [("plan", True), ("update", True)]


def test_plan_refusal_prevents_update_implementation(monkeypatch, capsys):
    called = False

    def fake_update(args):
        nonlocal called
        called = True

    def deny(args):
        raise deployment_plan.DeploymentMutationDenied(
            "image-managed is not updatable in place",
            remediation="pull and recreate",
        )

    monkeypatch.delenv("HERMES_DEPLOYMENT_PLAN", raising=False)
    monkeypatch.setattr(deployment_plan, "admit_update", deny)
    args = _parser(fake_update).parse_args(["update"])
    with pytest.raises(SystemExit) as exc:
        args.func(args)
    assert exc.value.code == 2
    assert called is False
    err = capsys.readouterr().err
    assert "not updatable in place" in err
    assert "pull and recreate" in err


def test_explicit_missing_plan_path_fails_closed(tmp_path):
    missing = tmp_path / "missing-plan.json"
    with pytest.raises(
        deployment_plan.DeploymentPlanInvalid,
        match="explicit deployment plan does not exist",
    ):
        validate_update_plan_source(
            {
                "HERMES_HOME": str(tmp_path / "home"),
                "HERMES_DEPLOYMENT_PLAN": str(missing),
            }
        )


def test_unknown_safety_bearing_plan_key_fails_closed(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    plan_path = home / "deployment-plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "cli-only",
                "kind": "git-venv",
                "canonical_checkout": str(tmp_path / "repo"),
                "allowed_component": ["source"],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(
        deployment_plan.DeploymentPlanInvalid,
        match="unknown deployment-plan keys: allowed_component",
    ):
        validate_update_plan_source({"HERMES_HOME": str(home)})


def test_legacy_update_refuses_narrowed_component_authority(tmp_path):
    plan = _mutable_plan(
        tmp_path,
        allowed_components=["source", "cli"],
    )
    with pytest.raises(
        deployment_plan.DeploymentMutationDenied,
        match="narrowed allowed_components",
    ):
        enforce_legacy_update_envelope(plan)


def test_legacy_update_refuses_narrowed_runtime_origin_authority(tmp_path):
    plan = _mutable_plan(
        tmp_path,
        allowed_runtime_origins=["external"],
    )
    with pytest.raises(
        deployment_plan.DeploymentMutationDenied,
        match="narrowed allowed_runtime_origins",
    ):
        enforce_legacy_update_envelope(plan)


def test_legacy_update_refuses_disabled_local_provisioning(tmp_path):
    plan = _mutable_plan(
        tmp_path,
        automatic_local_provisioning=False,
    )
    with pytest.raises(
        deployment_plan.DeploymentMutationDenied,
        match="forbids automatic local provisioning",
    ):
        enforce_legacy_update_envelope(plan)


def test_update_check_does_not_require_full_legacy_mutation_envelope(
    monkeypatch, tmp_path
):
    calls = []
    narrowed = _mutable_plan(
        tmp_path,
        allowed_components=["source", "cli"],
        automatic_local_provisioning=False,
    )

    def fake_admit(args):
        calls.append(("plan", args.check))
        return narrowed

    def fake_update(args):
        calls.append(("update", args.check))
        return 23

    monkeypatch.delenv("HERMES_DEPLOYMENT_PLAN", raising=False)
    monkeypatch.setattr(deployment_plan, "admit_update", fake_admit)
    args = _parser(fake_update).parse_args(["update", "--check"])
    assert args.func(args) == 23
    assert calls == [("plan", True), ("update", True)]


def test_update_plan_remains_observation_only(monkeypatch, tmp_path):
    calls = []
    narrowed = _mutable_plan(
        tmp_path,
        allowed_components=["source", "cli"],
        automatic_local_provisioning=False,
    )

    def fake_load():
        calls.append("plan")
        return narrowed

    def fake_update(args):
        calls.append(("update", args.plan))
        return 29

    monkeypatch.delenv("HERMES_DEPLOYMENT_PLAN", raising=False)
    monkeypatch.setattr(deployment_plan, "load_deployment_plan", fake_load)
    args = _parser(fake_update).parse_args(["update", "--plan"])
    assert args.func(args) == 29
    assert calls == ["plan", ("update", True)]
