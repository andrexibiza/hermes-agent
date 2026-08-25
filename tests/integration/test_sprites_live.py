"""Opt-in live API checks in a run-unique namespace."""

from __future__ import annotations

import os
import uuid

import pytest

from hermes_plugin_sprites import environment as module

pytestmark = pytest.mark.integration

_TOKEN = os.getenv("SPRITES_TOKEN") or os.getenv("SPRITE_TOKEN")
if not _TOKEN:
    pytest.skip("SPRITES_TOKEN not set", allow_module_level=True)

_RUN_ID = uuid.uuid4().hex[:8]


def _test_name(task_id: str) -> str:
    display = module._collapse_slug(task_id) or "default"
    return module._bounded_name(f"test-{_RUN_ID}-{display}", uuid.uuid4().hex[:12])


@pytest.fixture
def ephemeral(monkeypatch, request):
    monkeypatch.setattr(module, "_ephemeral_sprite_name", _test_name)
    env = module.SpritesEnvironment(
        task_id=f"live-{request.node.name}",
        persistent_filesystem=False,
    )
    yield env
    env.cleanup()


def _execute(env, command: str):
    result = env.execute(command)
    assert isinstance(result, dict)
    return result


def test_echo(ephemeral):
    result = _execute(ephemeral, "echo 'Hello from a Sprite!'")
    assert result["exit_code"] == 0
    assert "Hello from a Sprite!" in result["output"]


def test_nonzero_exit(ephemeral):
    assert _execute(ephemeral, "exit 42")["exit_code"] == 42


def test_filesystem_round_trip(ephemeral):
    _execute(ephemeral, "echo survive > /tmp/hermes-plugin-sprites.txt")
    result = _execute(ephemeral, "cat /tmp/hermes-plugin-sprites.txt")
    assert result["exit_code"] == 0
    assert "survive" in result["output"]


def test_persistent_resume_recreates_environment(monkeypatch):
    name = f"hermes-test-{_RUN_ID}-persistent"
    monkeypatch.setattr(module, "_resolve_sprite_name", lambda task_id: name)
    first = module.SpritesEnvironment(task_id="persistent", persistent_filesystem=True)
    second = None
    try:
        assert _execute(first, "echo survive > /tmp/hermes-plugin-resume.txt")["exit_code"] == 0
        first.cleanup()  # closes client, leaves the Sprite alive
        second = module.SpritesEnvironment(task_id="persistent", persistent_filesystem=True)
        result = _execute(second, "cat /tmp/hermes-plugin-resume.txt")
        assert result["exit_code"] == 0
        assert "survive" in result["output"]
    finally:
        target = second or first
        target._persistent = False
        target.cleanup()
