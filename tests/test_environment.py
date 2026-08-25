from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from hermes_plugin_sprites import environment as module


class NotFoundError(Exception):
    pass


class SpriteError(Exception):
    pass


class ExitError(Exception):
    def __init__(self, code, stdout=b"", stderr=b""):
        self._code = code
        self.stdout = stdout
        self.stderr = stderr

    def exit_code(self):
        return self._code


class TimeoutError(Exception):
    pass


def install_sdk(monkeypatch, client):
    sprites = types.ModuleType("sprites")
    sprites.SpritesClient = MagicMock(return_value=client)
    exceptions = types.ModuleType("sprites.exceptions")
    exceptions.NotFoundError = NotFoundError
    exceptions.SpriteError = SpriteError
    exceptions.ExitError = ExitError
    exceptions.TimeoutError = TimeoutError
    sprites.exceptions = exceptions
    monkeypatch.setitem(sys.modules, "sprites", sprites)
    monkeypatch.setitem(sys.modules, "sprites.exceptions", exceptions)
    return sprites


def make_sprite(name="hermes-default", home="/home/sprite"):
    sprite = MagicMock()
    sprite.name = name
    home_command = MagicMock()
    home_command.combined_output.return_value = f"{home}\n".encode()
    sprite.command.return_value = home_command
    sprite.filesystem.return_value = MagicMock()
    return sprite


def prepare(monkeypatch, *, client=None, token="token"):
    client = client or MagicMock()
    install_sdk(monkeypatch, client)
    monkeypatch.setenv("SPRITES_TOKEN", token)
    monkeypatch.delenv("SPRITE_TOKEN", raising=False)
    monkeypatch.setattr(module, "_resolve_profile_identity", lambda: None)
    return client


def test_missing_token_raises(monkeypatch):
    client = prepare(monkeypatch)
    monkeypatch.delenv("SPRITES_TOKEN")
    with pytest.raises(ValueError, match="SPRITES_TOKEN"):
        module.SpritesEnvironment(task_id="x")
    client.get_sprite.assert_not_called()


def test_missing_sdk_has_actionable_error(monkeypatch):
    monkeypatch.delitem(sys.modules, "sprites", raising=False)
    monkeypatch.delitem(sys.modules, "sprites.exceptions", raising=False)
    monkeypatch.setattr(module, "_load_sdk", lambda: (_ for _ in ()).throw(ImportError("sprites-py>=0.5.0,<0.6")))
    with pytest.raises(ImportError, match="sprites-py"):
        module.SpritesEnvironment(task_id="x")


def test_persistent_resumes_existing(monkeypatch):
    client = MagicMock()
    sprite = make_sprite("hermes-mine")
    client.get_sprite.return_value = sprite
    prepare(monkeypatch, client=client)
    env = module.SpritesEnvironment(task_id="mine", persistent_filesystem=True)
    client.get_sprite.assert_called_once_with("hermes-mine")
    client.create_sprite.assert_not_called()
    assert env.cwd == "/home/sprite"


def test_persistent_creates_after_not_found(monkeypatch):
    client = MagicMock()
    sprite = make_sprite("hermes-fresh")
    client.get_sprite.side_effect = NotFoundError("404")
    client.create_sprite.return_value = sprite
    prepare(monkeypatch, client=client)
    env = module.SpritesEnvironment(task_id="fresh", persistent_filesystem=True)
    client.create_sprite.assert_called_once_with("hermes-fresh")
    assert env._sprite is sprite


def test_create_race_adopts_winner(monkeypatch):
    client = MagicMock()
    winner = make_sprite("hermes-fresh")
    sequence = iter([NotFoundError("404"), winner])

    def get_sprite(name):
        result = next(sequence)
        if isinstance(result, Exception):
            raise result
        return result

    client.get_sprite.side_effect = get_sprite
    client.create_sprite.side_effect = SpriteError("already exists")
    prepare(monkeypatch, client=client)
    env = module.SpritesEnvironment(task_id="fresh", persistent_filesystem=True)
    assert env._sprite is winner
    assert client.get_sprite.call_count == 2


def test_genuine_create_error_surfaces(monkeypatch):
    client = MagicMock()
    client.get_sprite.side_effect = NotFoundError("404")
    client.create_sprite.side_effect = SpriteError("quota exceeded")
    prepare(monkeypatch, client=client)
    with pytest.raises(SpriteError, match="quota exceeded"):
        module.SpritesEnvironment(task_id="fresh", persistent_filesystem=True)


def test_ephemeral_never_gets_or_adopts(monkeypatch):
    client = MagicMock()
    sprite = make_sprite("ephemeral")
    client.create_sprite.return_value = sprite
    prepare(monkeypatch, client=client)
    env = module.SpritesEnvironment(task_id="mine", persistent_filesystem=False)
    client.get_sprite.assert_not_called()
    client.create_sprite.assert_called_once_with(env._sprite_name)
    assert env._sprite_name.startswith("hermes-eph-mine-")


def test_explicit_cwd_not_rewritten(monkeypatch):
    client = MagicMock()
    client.get_sprite.return_value = make_sprite()
    prepare(monkeypatch, client=client)
    env = module.SpritesEnvironment(cwd="/workspace", task_id="x")
    assert env.cwd == "/workspace"


def test_persistent_cleanup_leaves_sprite_and_closes_client(monkeypatch):
    client = MagicMock()
    sprite = make_sprite()
    client.get_sprite.return_value = sprite
    prepare(monkeypatch, client=client)
    env = module.SpritesEnvironment(task_id="x", persistent_filesystem=True)
    env.cleanup()
    sprite.delete.assert_not_called()
    client.close.assert_called_once()
    env.cleanup()
    client.close.assert_called_once()


def test_ephemeral_cleanup_deletes(monkeypatch):
    client = MagicMock()
    sprite = make_sprite()
    client.create_sprite.return_value = sprite
    prepare(monkeypatch, client=client)
    env = module.SpritesEnvironment(task_id="x", persistent_filesystem=False)
    env.cleanup()
    sprite.delete.assert_called_once()


def test_upload_uses_sprite_filesystem(monkeypatch, tmp_path):
    client = MagicMock()
    sprite = make_sprite()
    client.get_sprite.return_value = sprite
    prepare(monkeypatch, client=client)
    env = module.SpritesEnvironment(task_id="x")
    source = tmp_path / "secret"
    source.write_bytes(b"payload")
    remote = MagicMock()
    env._fs.__truediv__.return_value = remote
    env._sprite_upload(str(source), "/home/sprite/.hermes/secret")
    remote.parent.mkdir.assert_called_once_with(parents=True, exist_ok=True)
    remote.write_bytes.assert_called_once_with(b"payload")


def test_delete_failure_propagates(monkeypatch):
    client = MagicMock()
    sprite = make_sprite()
    client.get_sprite.return_value = sprite
    prepare(monkeypatch, client=client)
    env = module.SpritesEnvironment(task_id="x")
    remote = MagicMock()
    remote.unlink.side_effect = OSError("websocket dropped")
    env._fs.__truediv__.return_value = remote
    with pytest.raises(OSError, match="websocket dropped"):
        env._sprite_delete(["/home/sprite/.hermes/.env"])


def run_handle(env, command, *, timeout):
    handle = env._run_bash(command, timeout=timeout)
    handle.wait()
    return handle.stdout.read(), handle.returncode


def test_run_bash_success(monkeypatch):
    client = MagicMock()
    sprite = make_sprite()
    client.get_sprite.return_value = sprite
    prepare(monkeypatch, client=client)
    env = module.SpritesEnvironment(task_id="x")
    command = MagicMock()
    command.combined_output.return_value = b"ok\n"
    sprite.command = MagicMock(return_value=command)
    output, code = run_handle(env, "echo ok", timeout=10)
    assert (output, code) == ("ok\n", 0)
    assert sprite.command.call_args.kwargs["timeout"] == 10.0


def test_run_bash_nonzero(monkeypatch):
    client = MagicMock()
    sprite = make_sprite()
    client.get_sprite.return_value = sprite
    prepare(monkeypatch, client=client)
    env = module.SpritesEnvironment(task_id="x")
    command = MagicMock()
    command.combined_output.side_effect = ExitError(7, b"before\n")
    sprite.command = MagicMock(return_value=command)
    output, code = run_handle(env, "exit 7", timeout=10)
    assert "before" in output
    assert code == 7


def test_run_bash_timeout_maps_to_124(monkeypatch):
    client = MagicMock()
    sprite = make_sprite()
    client.get_sprite.return_value = sprite
    prepare(monkeypatch, client=client)
    env = module.SpritesEnvironment(task_id="x")
    command = MagicMock()
    command.combined_output.side_effect = TimeoutError("deadline")
    sprite.command = MagicMock(return_value=command)
    output, code = run_handle(env, "sleep 999", timeout=1)
    assert "timed out" in output
    assert code == 124


@pytest.mark.parametrize("timeout", [0, -1, None])
def test_run_bash_never_issues_unbounded_deadline(monkeypatch, timeout):
    client = MagicMock()
    sprite = make_sprite()
    client.get_sprite.return_value = sprite
    prepare(monkeypatch, client=client)
    env = module.SpritesEnvironment(task_id="x")
    command = MagicMock()
    command.combined_output.return_value = b""
    sprite.command = MagicMock(return_value=command)
    run_handle(env, "true", timeout=timeout)
    assert sprite.command.call_args.kwargs["timeout"] == 3600.0
