"""Standalone Sprites terminal backend for Hermes Agent."""

from __future__ import annotations

from typing import Any

from .environment import SpritesEnvironment
from .provider import SpritesProvider

__version__ = "0.1.0"


def register(ctx: Any) -> None:
    """Register Sprites as a first-class Hermes terminal environment."""
    ctx.register_terminal_environment_provider(SpritesProvider())


__all__ = ["SpritesEnvironment", "SpritesProvider", "register", "__version__"]
