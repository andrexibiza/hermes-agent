"""Sprites execution environment for Hermes Agent.

The Sprite name is a durable authority boundary: it selects a live VM, its
filesystem, processes, sockets, and billing lifecycle. Persistent names are
therefore profile-scoped, collision-resistant, and DNS-bounded. Ephemeral
runs mint unique names and never adopt an existing VM.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import uuid
from pathlib import Path
from typing import Any

from tools.environments.base import BaseEnvironment, _ThreadedProcessHandle
from tools.environments.file_sync import FileSyncManager, iter_sync_files

logger = logging.getLogger(__name__)

_MAX_NAME_LEN = 63
_DIGEST_LEN = 12
_SDK_REQUIREMENT = "sprites-py>=0.5.0,<0.6"


def _collapse_slug(value: str) -> str:
    """Collapse arbitrary text to a lowercase DNS-label component."""
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")


def _identity_digest(*parts: str) -> str:
    """Hash an ordered tuple using unambiguous length-prefixed components."""
    digest = hashlib.sha256()
    for part in parts:
        encoded = (part or "").encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()[:_DIGEST_LEN]


def _bounded_name(display: str, digest: str) -> str:
    """Compose ``hermes-{display}-{digest}`` without truncating identity."""
    budget = _MAX_NAME_LEN - len("hermes-") - 1 - len(digest)
    display = (display or "")[:budget].strip("-")
    return f"hermes-{display}-{digest}" if display else f"hermes-{digest}"


def _slugify_name_component(value: str) -> str:
    """Preserve clean legacy values; hash lossy transformations."""
    raw = value or ""
    slug = _collapse_slug(raw)
    if slug == raw:
        return slug
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:6]
    return f"{slug}-{digest}" if slug else digest


def _resolve_profile_identity() -> str | None:
    """Resolve the active Hermes profile without a fail-open default fallback."""
    try:
        from agent.file_safety import _hermes_home_path, _hermes_root_path

        home = _hermes_home_path().resolve()
        root = _hermes_root_path().resolve()
    except Exception as exc:
        raise RuntimeError(
            "Sprites backend could not resolve the active Hermes profile; "
            f"refusing to fall back to the default profile's Sprite: {exc}"
        ) from exc

    if home == root:
        return None
    try:
        relative = home.relative_to(root / "profiles")
    except ValueError:
        return f"home:{home}"

    name = relative.parts[0] if relative.parts else None
    if name is None or name == "default":
        return None
    return name


def _resolve_sprite_name(task_id: str) -> str:
    """Return the deterministic, profile-scoped persistent Sprite name."""
    task_id = task_id or ""
    profile = _resolve_profile_identity()

    if profile is None:
        task_slug = _slugify_name_component(task_id) or "default"
        legacy = f"hermes-{task_slug}"
        if len(legacy) <= _MAX_NAME_LEN:
            return legacy
        return _bounded_name(
            _collapse_slug(task_id),
            _identity_digest("", task_id),
        )

    display = f"{_collapse_slug(profile) or 'profile'}-{_collapse_slug(task_id) or 'default'}"
    return _bounded_name(display, _identity_digest(profile, task_id))


def _ephemeral_sprite_name(task_id: str) -> str:
    """Return a unique, non-resumable name for an ephemeral sandbox."""
    nonce = uuid.uuid4().hex[:12]
    display = _collapse_slug(task_id) or "default"
    return _bounded_name(f"eph-{display}", nonce)


def _load_sdk() -> tuple[Any, Any, Any]:
    try:
        from sprites import SpritesClient
        from sprites.exceptions import NotFoundError, SpriteError
    except ImportError as exc:
        raise ImportError(
            "Sprites backend requires the reviewed SDK range "
            f"{_SDK_REQUIREMENT}. Install it with: "
            f"python -m pip install '{_SDK_REQUIREMENT}'"
        ) from exc
    return SpritesClient, NotFoundError, SpriteError


class SpritesEnvironment(BaseEnvironment):
    """Stateful Fly.io Sprite satisfying Hermes' BaseEnvironment contract."""

    _stdin_mode = "heredoc"

    def __init__(
        self,
        cwd: str = "/root",
        timeout: int = 60,
        persistent_filesystem: bool = True,
        task_id: str = "default",
    ) -> None:
        requested_cwd = cwd
        super().__init__(cwd=cwd, timeout=timeout)

        SpritesClient, NotFoundError, SpriteError = _load_sdk()
        self._NotFoundError = NotFoundError
        self._SpriteError = SpriteError

        from agent.secret_scope import get_secret

        token = get_secret("SPRITES_TOKEN") or get_secret("SPRITE_TOKEN")
        if not token:
            raise ValueError(
                "Sprites backend requires SPRITES_TOKEN for the active Hermes profile "
                "(SPRITE_TOKEN is also accepted)."
            )

        self._client = SpritesClient(
            token=token,
            timeout=max(30.0, float(timeout)),
        )
        self._persistent = persistent_filesystem
        self._task_id = task_id
        self._lock = threading.Lock()
        self._sprite = None

        if persistent_filesystem:
            self._sprite_name = _resolve_sprite_name(task_id)
            try:
                self._sprite = self._client.get_sprite(self._sprite_name)
                logger.info(
                    "Sprites: resumed %s for task %s",
                    self._sprite.name,
                    task_id,
                )
            except NotFoundError:
                try:
                    self._sprite = self._client.create_sprite(self._sprite_name)
                    logger.info(
                        "Sprites: created %s for task %s",
                        self._sprite.name,
                        task_id,
                    )
                except SpriteError as create_error:
                    # Cross-process first-use race: adopt the exact-name winner.
                    try:
                        self._sprite = self._client.get_sprite(self._sprite_name)
                        logger.info(
                            "Sprites: adopted concurrently-created %s for task %s",
                            self._sprite.name,
                            task_id,
                        )
                    except NotFoundError:
                        raise create_error
        else:
            self._sprite_name = _ephemeral_sprite_name(task_id)
            # Never GET/adopt ephemeral identities. Each construction owns a
            # new sandbox and cleanup may safely delete it.
            self._sprite = self._client.create_sprite(self._sprite_name)
            logger.info(
                "Sprites: created ephemeral %s for task %s",
                self._sprite.name,
                task_id,
            )

        self._remote_home = "/root"
        try:
            command = self._sprite.command("bash", "-c", "echo $HOME", timeout=15)
            detected_home = command.combined_output().decode().strip()
            if detected_home:
                self._remote_home = detected_home
                if requested_cwd in {"~", "/root"}:
                    self.cwd = detected_home
        except Exception:
            # Home discovery is advisory. /root remains a safe Linux fallback.
            pass

        self._fs = self._sprite.filesystem("/")
        self._sync_manager = FileSyncManager(
            get_files_fn=lambda: iter_sync_files(f"{self._remote_home}/.hermes"),
            upload_fn=self._sprite_upload,
            delete_fn=self._sprite_delete,
        )
        self._sync_manager.sync(force=True)
        self.init_session()

    def _sprite_upload(self, host_path: str, remote_path: str) -> None:
        data = Path(host_path).read_bytes()
        remote = self._fs / remote_path
        remote.parent.mkdir(parents=True, exist_ok=True)
        remote.write_bytes(data)

    def _sprite_delete(self, remote_paths: list[str]) -> None:
        # Non-missing failures deliberately propagate so FileSyncManager can
        # roll back its deletion transaction and retry later.
        for remote_path in remote_paths:
            (self._fs / remote_path).unlink(missing_ok=True)

    def _before_execute(self) -> None:
        self._sync_manager.sync()

    def _run_bash(
        self,
        cmd_string: str,
        *,
        login: bool = False,
        timeout: int = 120,
        stdin_data: str | None = None,
    ) -> _ThreadedProcessHandle:
        del stdin_data
        sprite = self._sprite
        from sprites.exceptions import ExitError, TimeoutError as SpritesTimeout

        shell_command = ["bash", "-l", "-c", cmd_string] if login else [
            "bash",
            "-c",
            cmd_string,
        ]
        # The SDK has no external kill hook. Never issue an unbounded paid exec.
        command_timeout = float(timeout) if timeout and timeout > 0 else 3600.0

        def execute() -> tuple[str, int]:
            command = sprite.command(*shell_command, timeout=command_timeout)
            try:
                output = command.combined_output()
                return output.decode("utf-8", errors="replace"), 0
            except ExitError as exc:
                combined = (exc.stdout or b"") + (exc.stderr or b"")
                exit_code = (
                    exc.exit_code()
                    if callable(getattr(exc, "exit_code", None))
                    else 1
                )
                return combined.decode("utf-8", errors="replace"), exit_code
            except SpritesTimeout:
                return f"command timed out after {command_timeout}s\n", 124

        return _ThreadedProcessHandle(execute, cancel_fn=None)

    def cleanup(self) -> None:
        with self._lock:
            if self._sprite is None:
                return
            try:
                if self._persistent:
                    logger.info("Sprites: leaving %s running (persistent)", self._sprite.name)
                else:
                    self._sprite.delete()
                    logger.info("Sprites: deleted %s", self._sprite.name)
            except Exception as exc:
                logger.warning("Sprites: cleanup failed for %s: %s", self._sprite_name, exc)
            finally:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._sprite = None
