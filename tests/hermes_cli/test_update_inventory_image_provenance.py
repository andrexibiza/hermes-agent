from __future__ import annotations

import json

import hermes_cli.update_inventory as inventory


def _marker(path):
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "deployment_kind": "image",
                "manager": "docker",
                "image": "nousresearch/hermes-agent",
                "version": "0.20.5",
                "revision": "b" * 40,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_baked_image_marker_outranks_bind_mounted_git_checkout(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    (checkout / ".install_method").write_text("git\n", encoding="utf-8")
    marker = _marker(tmp_path / "image-provenance.json")

    plan = inventory.collect_runtime_inventory(
        project_root=checkout,
        provenance_path=marker,
        include_runtimes=False,
    )

    assert plan.install_method == "docker"
    assert plan.deployment_kind == "image"
    assert plan.updatable_in_place is False
    assert plan.classification_reason == "baked_image_provenance"
    assert plan.image_provenance["revision"] == "b" * 40
    assert plan.image_provenance["marker_path"] == str(marker)


def test_marker_absence_preserves_git_behavior(monkeypatch, tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    monkeypatch.setattr("hermes_cli.config.get_managed_system", lambda: None)

    plan = inventory.collect_runtime_inventory(
        project_root=checkout,
        provenance_path=tmp_path / "missing.json",
        include_runtimes=False,
    )

    assert plan.install_method == "git"
    assert plan.deployment_kind == "mutable"
    assert plan.updatable_in_place is True
    assert plan.image_provenance is None


def test_invalid_present_marker_still_refuses_closed(tmp_path):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    marker = tmp_path / "image-provenance.json"
    marker.write_text("[]", encoding="utf-8")

    plan = inventory.collect_runtime_inventory(
        project_root=checkout,
        provenance_path=marker,
        include_runtimes=False,
    )

    assert plan.install_method == "docker"
    assert plan.deployment_kind == "image"
    assert plan.updatable_in_place is False
    assert plan.classification_reason == "invalid_baked_image_provenance"
    assert plan.image_provenance["valid"] is False
