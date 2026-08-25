from __future__ import annotations

from unittest.mock import MagicMock

from hermes_plugin_sprites import register
from hermes_plugin_sprites import provider as module


def test_registration_uses_terminal_provider_surface():
    ctx = MagicMock()
    register(ctx)
    ctx.register_terminal_environment_provider.assert_called_once()
    provider = ctx.register_terminal_environment_provider.call_args.args[0]
    assert isinstance(provider, module.SpritesProvider)


def test_provider_classification_contract():
    provider = module.SpritesProvider()
    assert provider.name == "sprites"
    assert provider.display_name == "Sprites"
    assert provider.is_remote is True
    assert provider.is_container is True
    assert provider.skip_container_guards is True
    assert provider.session_isolated_when_nonpersistent is True
    assert provider.cache_path_base == "~/.hermes"
    assert provider.strip_env_keys == frozenset({"SPRITES_TOKEN", "SPRITE_TOKEN"})
    assert "Fly.io" in provider.env_description


def test_probe_reports_missing_sdk(monkeypatch):
    monkeypatch.setattr(module, "_sdk_available", lambda: False)
    monkeypatch.setattr(module, "_get_token", lambda: "token")
    status, detail = module.SpritesProvider().probe()
    assert status == "needs_setup"
    assert "sprites-py>=0.5.0,<0.6" in detail


def test_probe_reports_missing_token(monkeypatch):
    monkeypatch.setattr(module, "_sdk_available", lambda: True)
    monkeypatch.setattr(module, "_get_token", lambda: None)
    status, detail = module.SpritesProvider().probe()
    assert status == "needs_setup"
    assert "SPRITES_TOKEN" in detail


def test_probe_ready(monkeypatch):
    monkeypatch.setattr(module, "_sdk_available", lambda: True)
    monkeypatch.setattr(module, "_get_token", lambda: "token")
    assert module.SpritesProvider().probe() == ("ready", "")


def test_requirements_need_both_sdk_and_token(monkeypatch):
    monkeypatch.setattr(module, "_sdk_available", lambda: True)
    monkeypatch.setattr(module, "_get_token", lambda: "token")
    assert module.SpritesProvider().check_requirements({}) is True
    monkeypatch.setattr(module, "_get_token", lambda: None)
    assert module.SpritesProvider().check_requirements({}) is False


def test_doctor_has_independent_rows(monkeypatch):
    monkeypatch.setattr(module, "_sdk_available", lambda: True)
    monkeypatch.setattr(module, "_get_token", lambda: None)
    rows = module.SpritesProvider().doctor_checks()
    assert rows[0][0] is True
    assert rows[1][0] is False


def test_create_maps_persistence_and_ignores_additive_kwargs(monkeypatch):
    constructed = MagicMock()
    monkeypatch.setattr(module, "SpritesEnvironment", constructed)
    provider = module.SpritesProvider()
    provider.create_environment(
        cwd="/workspace",
        timeout=17,
        task_id="session-1",
        image="ignored",
        container_config={"container_persistent": False, "container_cpu": 8},
        future_argument="ignored",
    )
    constructed.assert_called_once_with(
        cwd="/workspace",
        timeout=17,
        persistent_filesystem=False,
        task_id="session-1",
    )


def test_create_normalizes_string_false(monkeypatch):
    constructed = MagicMock()
    monkeypatch.setattr(module, "SpritesEnvironment", constructed)
    module.SpritesProvider().create_environment(
        cwd="/root",
        timeout=60,
        container_config={"container_persistent": "false"},
    )
    assert constructed.call_args.kwargs["persistent_filesystem"] is False


def test_setup_instructions_include_pinned_sdk_and_token():
    text = "\n".join(module.SpritesProvider().setup_instructions())
    assert "sprites-py>=0.5.0,<0.6" in text
    assert "SPRITES_TOKEN" in text
    assert "hermes-" in text
