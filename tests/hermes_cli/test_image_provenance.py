from __future__ import annotations

import json

from hermes_cli.image_provenance import (
    IMAGE_PROVENANCE_SCHEMA,
    read_image_provenance,
)


def _write(path, **overrides):
    payload = {
        "schema": IMAGE_PROVENANCE_SCHEMA,
        "deployment_kind": "image",
        "manager": "docker",
        "image": "nousresearch/hermes-agent",
        "version": "0.20.5",
        "revision": "a" * 40,
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_absent_marker_preserves_non_image_install(tmp_path):
    assert read_image_provenance(tmp_path / "missing.json") is None


def test_valid_marker_exposes_baked_identity(tmp_path):
    marker = _write(tmp_path / "image.json")
    provenance = read_image_provenance(marker)

    assert provenance is not None
    assert provenance.valid is True
    assert provenance.deployment_kind == "image"
    assert provenance.manager == "docker"
    assert provenance.version == "0.20.5"
    assert provenance.revision == "a" * 40
    assert provenance.marker_path == str(marker)


def test_present_malformed_marker_fails_closed(tmp_path):
    marker = tmp_path / "image.json"
    marker.write_text("{not-json", encoding="utf-8")

    provenance = read_image_provenance(marker)

    assert provenance is not None
    assert provenance.deployment_kind == "image"
    assert provenance.valid is False
    assert provenance.error == "marker_unreadable:JSONDecodeError"


def test_marker_classification_ignores_runtime_env(monkeypatch, tmp_path):
    marker = _write(tmp_path / "image.json")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "other-home"))
    monkeypatch.setenv("HERMES_MANAGED", "false")
    monkeypatch.setenv("HERMES_INSTALL_METHOD", "git")

    provenance = read_image_provenance(marker)

    assert provenance is not None
    assert provenance.deployment_kind == "image"
    assert provenance.manager == "docker"
