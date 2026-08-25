from __future__ import annotations

import re

import pytest

from hermes_plugin_sprites import environment as module


def _profile(monkeypatch, value):
    monkeypatch.setattr(module, "_resolve_profile_identity", lambda: value)


def test_default_profile_preserves_legacy_names(monkeypatch):
    _profile(monkeypatch, None)
    assert module._resolve_sprite_name("default") == "hermes-default"
    assert module._resolve_sprite_name("mytask") == "hermes-mytask"


def test_named_profile_literals_are_stable(monkeypatch):
    _profile(monkeypatch, "work")
    assert module._resolve_sprite_name("default") == "hermes-work-default-a092ad600654"
    assert module._resolve_sprite_name("mytask") == "hermes-work-mytask-0bba1287573c"


def test_independent_profiles_do_not_collide(monkeypatch):
    _profile(monkeypatch, "alpha")
    alpha = module._resolve_sprite_name("default")
    _profile(monkeypatch, "beta")
    beta = module._resolve_sprite_name("default")
    assert alpha == "hermes-alpha-default-f763b5cbf547"
    assert beta == "hermes-beta-default-8ab8e3ddf43d"
    assert alpha != beta


def test_component_boundaries_do_not_collide(monkeypatch):
    _profile(monkeypatch, "a-b")
    left = module._resolve_sprite_name("c")
    _profile(monkeypatch, "a")
    right = module._resolve_sprite_name("b-c")
    assert left == "hermes-a-b-c-78208f2b509c"
    assert right == "hermes-a-b-c-590074485363"
    assert left != right


def test_separator_forgery_does_not_collide(monkeypatch):
    _profile(monkeypatch, "a\x1fb")
    left = module._resolve_sprite_name("c")
    _profile(monkeypatch, "a")
    right = module._resolve_sprite_name("b\x1fc")
    assert left != right


def test_lossy_profiles_do_not_collide(monkeypatch):
    _profile(monkeypatch, "team_prod")
    underscore = module._resolve_sprite_name("default")
    _profile(monkeypatch, "team-prod")
    hyphen = module._resolve_sprite_name("default")
    assert underscore == "hermes-team-prod-default-e11db62b701d"
    assert hyphen == "hermes-team-prod-default-264eccd5d608"
    assert underscore != hyphen


def test_messy_components_are_dns_safe_and_pinned(monkeypatch):
    _profile(monkeypatch, "Team.Prod")
    name = module._resolve_sprite_name("sub agent_42")
    assert name == "hermes-team-prod-sub-agent-42-ca9aa4d02adc"
    assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)


@pytest.mark.parametrize(
    ("profile", "task"),
    [
        ("home:/Users/someone/Library/Application Support/custom-hermes-home", "default"),
        ("work", "session:agent:main:telegram:" + "x" * 60),
        (None, "session:agent:main:telegram:" + "x" * 60),
    ],
)
def test_names_are_bounded_and_keep_digest(monkeypatch, profile, task):
    _profile(monkeypatch, profile)
    name = module._resolve_sprite_name(task)
    assert len(name) <= module._MAX_NAME_LEN
    assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name)
    assert name.endswith(module._identity_digest(profile or "", task))


def test_empty_task_uses_default(monkeypatch):
    _profile(monkeypatch, None)
    assert module._resolve_sprite_name("") == "hermes-default"


def test_ephemeral_names_are_unique_and_bounded():
    first = module._ephemeral_sprite_name("session:" + "x" * 100)
    second = module._ephemeral_sprite_name("session:" + "x" * 100)
    assert first != second
    assert len(first) <= module._MAX_NAME_LEN
    assert len(second) <= module._MAX_NAME_LEN


def test_real_profile_path_resolution(monkeypatch, tmp_path):
    import agent.file_safety as file_safety

    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setattr(file_safety, "_hermes_root_path", lambda: root)

    monkeypatch.setattr(file_safety, "_hermes_home_path", lambda: root)
    assert module._resolve_profile_identity() is None

    default = root / "profiles" / "default"
    default.mkdir(parents=True)
    monkeypatch.setattr(file_safety, "_hermes_home_path", lambda: default)
    assert module._resolve_profile_identity() is None

    work = root / "profiles" / "work"
    work.mkdir()
    monkeypatch.setattr(file_safety, "_hermes_home_path", lambda: work)
    assert module._resolve_profile_identity() == "work"

    custom = tmp_path / "custom-home"
    custom.mkdir()
    monkeypatch.setattr(file_safety, "_hermes_home_path", lambda: custom)
    assert module._resolve_profile_identity() == f"home:{custom.resolve()}"


def test_profile_resolution_failure_fails_closed(monkeypatch):
    import agent.file_safety as file_safety

    def explode():
        raise OSError("no home")

    monkeypatch.setattr(file_safety, "_hermes_home_path", explode)
    with pytest.raises(RuntimeError, match="refusing to fall back"):
        module._resolve_sprite_name("x")
