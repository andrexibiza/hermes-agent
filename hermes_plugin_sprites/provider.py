"""Hermes terminal-environment provider for Fly.io Sprites."""

from __future__ import annotations

import importlib.util
import logging
import sys
from typing import Any, Dict, List, Optional, Tuple

try:
    from agent.terminal_env_provider import TerminalEnvironmentProvider
except ImportError as exc:  # pragma: no cover - exercised by old Hermes installs
    raise RuntimeError(
        "hermes-plugin-sprites requires a Hermes Agent release containing "
        "the terminal environment plugin API from NousResearch/hermes-agent#94400"
    ) from exc

from .environment import SpritesEnvironment

logger = logging.getLogger(__name__)

_SDK_REQUIREMENT = "sprites-py>=0.5.0,<0.6"
_TOKEN_KEYS = ("SPRITES_TOKEN", "SPRITE_TOKEN")


def _sdk_available() -> bool:
    """Return whether the reviewed sprites-py SDK is importable, without importing it."""
    if "sprites" in sys.modules:
        return True
    try:
        return importlib.util.find_spec("sprites") is not None
    except (ImportError, ValueError):
        return False


def _get_token() -> Optional[str]:
    """Read the active profile's token through Hermes' secret scope."""
    try:
        from agent.secret_scope import get_secret

        return get_secret("SPRITES_TOKEN") or get_secret("SPRITE_TOKEN")
    except Exception:
        return None


def _as_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


class SpritesProvider(TerminalEnvironmentProvider):
    """Declarative provider consumed by Hermes' terminal backend registry."""

    name = "sprites"
    display_name = "Sprites"
    is_remote = True
    is_container = True
    session_isolated_when_nonpersistent = True

    @property
    def description(self) -> str:
        return "Stateful cloud sandboxes on Fly.io with checkpoint and restore."

    @property
    def skip_container_guards(self) -> bool:
        # Sprites cannot mount the host filesystem; destructive commands are
        # confined to a disposable or explicitly persistent remote sandbox.
        return True

    @property
    def cache_path_base(self) -> str:
        # SpritesEnvironment discovers the actual remote $HOME; tilde remains
        # correct across root- and non-root-homed Sprite images.
        return "~/.hermes"

    @property
    def strip_env_keys(self) -> frozenset[str]:
        # Both accepted aliases are authority-bearing credentials and must be
        # absent from every model-authored subprocess.
        return frozenset(_TOKEN_KEYS)

    @property
    def env_description(self) -> str:
        return "a Sprite — a stateful cloud sandbox on Fly.io (Linux)"

    def is_available(self) -> bool:
        return _sdk_available() and bool(_get_token())

    def check_requirements(self, config: Dict[str, Any]) -> bool:
        del config
        sdk_ok = _sdk_available()
        token_ok = bool(_get_token())
        if not sdk_ok:
            logger.error(
                "Sprites backend requires %s; install it with: python -m pip install '%s'",
                _SDK_REQUIREMENT,
                _SDK_REQUIREMENT,
            )
        if not token_ok:
            logger.error(
                "Sprites backend requires SPRITES_TOKEN (SPRITE_TOKEN is also accepted)"
            )
        return sdk_ok and token_ok

    def probe(self) -> Tuple[str, str]:
        try:
            if not _sdk_available():
                return (
                    "needs_setup",
                    f"Install {_SDK_REQUIREMENT}: python -m pip install '{_SDK_REQUIREMENT}'",
                )
            if not _get_token():
                return (
                    "needs_setup",
                    "Set SPRITES_TOKEN for the active Hermes profile.",
                )
            return ("ready", "")
        except Exception as exc:  # pragma: no cover - defensive UI boundary
            return ("unavailable", f"Sprites probe failed: {exc}")

    def setup_instructions(self) -> List[str]:
        return [
            f"Install the SDK: python -m pip install '{_SDK_REQUIREMENT}'",
            "Create a token at https://sprites.dev or with `sprite login`.",
            "Store it as SPRITES_TOKEN in the active Hermes profile's .env file.",
            "A restricted token scoped to the `hermes-` prefix is recommended.",
        ]

    def doctor_checks(self) -> List[Tuple[bool, str, str]]:
        sdk_ok = _sdk_available()
        token_ok = bool(_get_token())
        return [
            (
                sdk_ok,
                "Sprites SDK",
                f"({_SDK_REQUIREMENT} installed)" if sdk_ok else f"(missing {_SDK_REQUIREMENT})",
            ),
            (
                token_ok,
                "Sprites token",
                "(configured for active profile)" if token_ok else "(SPRITES_TOKEN missing)",
            ),
        ]

    def create_environment(
        self,
        *,
        cwd: str,
        timeout: int,
        task_id: str = "default",
        image: Optional[str] = None,
        container_config: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> SpritesEnvironment:
        # Sprites currently owns image/compute sizing. Accept and deliberately
        # ignore these arguments so the provider remains forward-compatible
        # with Hermes' additive factory contract.
        del image, kwargs
        persistent = True
        if container_config is not None:
            persistent = _as_bool(
                container_config.get("container_persistent"),
                default=True,
            )
        return SpritesEnvironment(
            cwd=cwd or "/root",
            timeout=timeout,
            persistent_filesystem=persistent,
            task_id=task_id,
        )
