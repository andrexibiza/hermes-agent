from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def test_directory_plugin_loads_as_namespaced_package():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "hermes_external_plugin_sprites_test",
        root / "__init__.py",
        submodule_search_locations=[str(root)],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    assert callable(module.register)


def test_extraction_does_not_recreate_core_dispatch_branches():
    root = Path(__file__).resolve().parents[1]
    production = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (root / "hermes_plugin_sprites").glob("*.py")
    )
    assert "tools.lazy_deps" not in production
    assert "tools.terminal_tool" not in production
    assert "TERMINAL_ENV" not in production
    assert "register_terminal_environment_provider" in (
        root / "hermes_plugin_sprites" / "__init__.py"
    ).read_text(encoding="utf-8")


def test_pip_entry_point_is_declared():
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    assert '[project.entry-points."hermes_agent.plugins"]' in pyproject
    assert 'sprites = "hermes_plugin_sprites:register"' in pyproject
