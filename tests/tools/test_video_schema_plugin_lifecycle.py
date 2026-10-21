"""Schema inventory must not reload unrelated plugins; generation retains late discovery."""

import json
from textwrap import dedent

import hermes_yaml as yaml
import pytest

from agent import video_gen_registry
from hermes_cli import plugins
from tools import video_generation_tool
from tools.registry import registry


def _write_provider_plugin(home, name, providers):
    directory = home / "plugins" / name
    directory.mkdir(parents=True)
    (directory / "plugin.yaml").write_text(
        yaml.safe_dump({"name": name, "version": "1.0.0", "description": "Lifecycle fixture"}),
        encoding="utf-8",
    )
    events = home / "lifecycle.log"
    (directory / "__init__.py").write_text(
        dedent(f"""\
        from pathlib import Path
        from agent.video_gen_provider import VideoGenProvider

        def record(event):
            with Path({str(events)!r}).open("a", encoding="utf-8") as stream:
                stream.write(event + "\\n")

        class Provider(VideoGenProvider):
            def __init__(self, name):
                self._name = name

            @property
            def name(self):
                return self._name

            def is_available(self):
                return True

            def generate(self, prompt, **kwargs):
                return {{"success": True, "provider": self.name, "prompt": prompt,
                         "video": "https://example.invalid/generated.mp4"}}

        def register(ctx):
            record({name!r} + ":load")
            ctx.on_unload(lambda: record({name!r} + ":unload"))
            for name in {providers!r}:
                ctx.register_video_gen_provider(Provider(name))
        """),
        encoding="utf-8",
    )


@pytest.fixture
def plugin_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(plugins, "get_bundled_plugins_dir", lambda: tmp_path / "bundled")
    monkeypatch.setattr(plugins.PluginManager, "_scan_entry_points", lambda self: [])
    video_gen_registry._reset_for_tests()
    yield tmp_path
    plugins._reset_plugin_managers_for_tests()
    video_gen_registry._reset_for_tests()


@pytest.mark.parametrize("configured", [None, "not-installed"])
def test_schema_inventory_preserves_loaded_plugin_instances(plugin_home, configured):
    config = {"plugins": {"enabled": ["inventory-fixture"]}}
    if configured is not None:
        config["video_gen"] = {"provider": configured}
    (plugin_home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    _write_provider_plugin(plugin_home, "inventory-fixture", ["first", "second"])
    plugins._ensure_plugins_discovered()
    original = video_gen_registry.get_provider("first")
    assert original is not None
    events = plugin_home / "lifecycle.log"
    before = events.read_text(encoding="utf-8")

    for _ in range(3):
        schemas = registry.get_definitions({"video_generate"})
        assert schemas and "No video backend is available" in schemas[0]["function"]["description"]

    assert events.read_text(encoding="utf-8") == before
    assert video_gen_registry.get_provider("first") is original


def test_generation_still_discovers_a_plugin_installed_after_initial_inventory(plugin_home):
    (plugin_home / "config.yaml").write_text(
        yaml.safe_dump({"plugins": {"enabled": ["inventory-fixture", "late-fixture"]},
                        "video_gen": {"provider": "late"}}),
        encoding="utf-8",
    )
    _write_provider_plugin(plugin_home, "inventory-fixture", [])
    plugins._ensure_plugins_discovered()
    _write_provider_plugin(plugin_home, "late-fixture", ["late"])
    events = plugin_home / "lifecycle.log"
    before = events.read_text(encoding="utf-8")

    schema = video_generation_tool._build_dynamic_video_schema()
    assert "No video backend is available" in schema["description"]
    assert events.read_text(encoding="utf-8") == before
    assert video_gen_registry.get_provider("late") is None

    result = json.loads(registry.dispatch("video_generate", {"prompt": "A quiet landscape"}))
    assert result["success"] is True
    assert result["provider"] == "late"
    assert result["prompt"] == "A quiet landscape"
    assert video_gen_registry.get_provider("late") is not None
