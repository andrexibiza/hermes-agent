from pathlib import Path

import yaml


def test_manifest_declares_native_backend_and_bounded_dependency():
    manifest = yaml.safe_load(Path("plugin.yaml").read_text())
    assert manifest["manifest_version"] == 2
    assert manifest["api_version"] == 1
    assert manifest["name"] == "sprites"
    assert manifest["kind"] == "backend"
    assert manifest["python_dependencies"] == ["sprites-py>=0.5.0,<0.6"]


def test_manifest_does_not_gate_accepted_token_alias():
    manifest = yaml.safe_load(Path("plugin.yaml").read_text())
    # A requires_env gate on SPRITES_TOKEN would prevent the documented
    # SPRITE_TOKEN alias from loading far enough for the provider to accept it.
    assert "requires_env" not in manifest
