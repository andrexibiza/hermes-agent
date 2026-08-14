"""Freemaxxing routing-authority invariants."""

import importlib
import inspect


def _plugin_module():
    from providers import get_provider_profile

    assert get_provider_profile("freemaxxing") is not None
    return importlib.import_module("plugins.model_providers.freemaxxing")


def test_freemaxxing_alias_remains_opaque(monkeypatch):
    import hermes_cli.model_switch as model_switch

    plugin = _plugin_module()
    starts = []
    monkeypatch.setattr(
        plugin,
        "ensure_proxy",
        lambda: starts.append(True) or "http://127.0.0.1:11435/v1",
    )

    assert model_switch.resolve_alias("freemaxxing", "openrouter") == (
        "freemaxxing",
        "freemaxxing",
        "freemaxxing",
    )
    assert model_switch.resolve_alias("fm", "nous") == (
        "freemaxxing",
        "freemaxxing",
        "fm",
    )
    assert starts == [True, True]
    assert not hasattr(model_switch, "_discover_freemaxxing_model")
    assert not hasattr(model_switch, "_FREEMAXXING_CACHE")


def test_picker_row_does_not_select_a_vendor_model(monkeypatch):
    import agent.models_dev as models_dev
    from hermes_cli.inventory import _freemaxxing_provider_row

    plugin = _plugin_module()
    starts = []
    monkeypatch.setattr(
        plugin,
        "ensure_proxy",
        lambda: starts.append(True) or "http://127.0.0.1:11435/v1",
    )
    monkeypatch.setattr(
        models_dev,
        "list_provider_models",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("core must not inspect vendor catalogs for freemaxxing")
        ),
    )

    row = _freemaxxing_provider_row("openrouter")
    assert row is not None
    assert row["slug"] == "freemaxxing"
    assert row["models"] == ["freemaxxing"]
    assert row["auth_type"] == "virtual"
    assert starts == [True]


def test_agent_init_does_not_rewrite_freemaxxing_to_native_provider():
    from agent.agent_init import init_agent

    source = inspect.getsource(init_agent)
    assert 'raw_input="freemaxxing"' not in source
    assert "_discover_freemaxxing_model" not in source
