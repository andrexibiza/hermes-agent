from __future__ import annotations

import json

import hermes_cli.update_receipt as receipts
from hermes_cli.update_contract import (
    IMAGE_MANAGED_UPDATE_REFUSED,
    UPDATE_REFUSED_EXIT,
    perform_update,
)
from hermes_cli.update_inventory import UpdatePlan


def _image_plan():
    return UpdatePlan(
        install_method="docker",
        deployment_kind="image",
        classification_reason="baked_image_provenance",
        image_provenance={
            "schema": 1,
            "deployment_kind": "image",
            "manager": "docker",
            "image": "nousresearch/hermes-agent",
            "version": "0.20.5",
            "revision": "c" * 40,
            "marker_path": "/etc/hermes/image-provenance.json",
            "valid": True,
            "error": None,
        },
        updatable_in_place=False,
        update_mechanism="docker pull nousresearch/hermes-agent:latest",
        expected_sha="d" * 40,
        expected_version="0.20.5",
    )


def test_refusal_is_stable_and_durable(monkeypatch, tmp_path):
    receipt_dir = tmp_path / "receipts"
    monkeypatch.setattr(receipts, "_receipt_dir", lambda: receipt_dir)
    receipts._current = None

    refusal = perform_update(
        surface="cli",
        requested_target="main",
        plan=_image_plan(),
    )

    assert refusal is not None
    assert refusal.code == IMAGE_MANAGED_UPDATE_REFUSED
    assert UPDATE_REFUSED_EXIT == 2
    assert refusal.surface == "cli"
    assert refusal.requested_target == "main"
    assert refusal.baked_identity["revision"] == "c" * 40
    assert "image-managed" in refusal.message
    assert "docker pull nousresearch/hermes-agent:latest" in refusal.message

    payload = json.loads((receipt_dir / "latest.json").read_text(encoding="utf-8"))
    assert payload["outcome"] == "refused"
    assert payload["stop_reason"] == IMAGE_MANAGED_UPDATE_REFUSED
    assert payload["surface"] == "cli"
    assert payload["requested_target"] == "main"
    assert len(payload["correlation_id"]) == 32
    assert payload["refusal"]["code"] == IMAGE_MANAGED_UPDATE_REFUSED
    assert payload["plan"]["deployment_kind"] == "image"


def test_mutable_plan_is_admitted_without_receipt(monkeypatch, tmp_path):
    receipt_dir = tmp_path / "receipts"
    monkeypatch.setattr(receipts, "_receipt_dir", lambda: receipt_dir)
    receipts._current = None
    plan = UpdatePlan(
        install_method="git",
        deployment_kind="mutable",
        classification_reason="install_method:git",
        updatable_in_place=True,
    )

    assert perform_update(surface="cli", plan=plan) is None
    assert receipts._current is None
    assert not receipt_dir.exists()


def test_refusal_decision_has_no_network_or_subprocess_dependency(
    monkeypatch, tmp_path
):
    marker = tmp_path / "image.json"
    marker.write_text(
        json.dumps(
            {
                "schema": 1,
                "deployment_kind": "image",
                "manager": "docker",
                "version": "0.20.5",
                "revision": "e" * 40,
            }
        ),
        encoding="utf-8",
    )
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    monkeypatch.setattr(
        "subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("admission must not spawn subprocesses")
        ),
    )
    monkeypatch.setattr(receipts, "_receipt_dir", lambda: tmp_path / "receipts")
    receipts._current = None

    refusal = perform_update(
        surface="dashboard_api",
        project_root=checkout,
        provenance_path=marker,
    )

    assert refusal is not None
    assert refusal.code == IMAGE_MANAGED_UPDATE_REFUSED
