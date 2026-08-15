"""Profile-scoped secret resolution.

Per the design spec §7.2 and §3.1: secrets are resolved under the
active Hermes profile and account alias.  A scoped miss returns a miss
— it never borrows an unscoped environment value from another profile.

This module provides a lightweight scoped-secret wrapper that works
with or without the host ``agent.secret_scope`` module.  When running
inside Hermes, it delegates to the host's secret machinery; when
running standalone (tests, CLI), it falls back to a simple env-var
reader with the same scoping semantics.
"""

from __future__ import annotations

import os
from typing import Optional


def _try_host_scoped_get(name: str, default: Optional[str] = None) -> Optional[str]:
    """Attempt to use Hermes' host-scoped secret resolver.

    Returns the secret if the host module is available and the secret
    exists.  Returns ``None`` otherwise (so the caller can apply its
    own fallback logic).  Never raises on import failure.
    """
    try:
        from agent.secret_scope import get_secret as _get_secret
        from agent.secret_scope import UnscopedSecretError
    except ImportError:
        return None

    try:
        return _get_secret(name)
    except UnscopedSecretError:
        return None
    except Exception:
        # Any other host-side error is treated as a miss — the caller's
        # fallback takes over.
        return None


def get_scoped_secret(
    name: str,
    default: Optional[str] = None,
    *,
    _env_fallback: bool = True,
) -> Optional[str]:
    """Read a profile-scoped secret.

    Resolution order:
    1. Host scoped secret resolver (``agent.secret_scope``) — if available
       and the secret is registered under the active profile's scope.
    2. ``os.environ`` — only when ``_env_fallback`` is True AND we are on
       the default profile (env is that profile's own value).  Under a
       non-default profile, a scoped miss does NOT fall back to env,
       because env may hold another profile's value.

    Args:
        name: Secret/env var name.
        default: Returned on miss.
        _env_fallback: Internal flag — set False to disable env fallback
            entirely (used in account-level secret resolution where the
            caller has already confirmed scoping).

    Returns:
        The secret value, or ``default`` if not found.
    """
    # Try host-scoped resolution first
    host_val = _try_host_scoped_get(name, default=None)
    if host_val is not None:
        return host_val
    if host_val is not None:
        return host_val

    # Determine if we're on the default profile
    is_default = _is_default_profile()

    # Env fallback — only for default profile
    if _env_fallback and is_default:
        val = os.environ.get(name)
        if val is not None and val != "":
            return val

    return default


def _is_default_profile() -> bool:
    """Check if we're on the default Hermes profile (no multiplexing)."""
    try:
        from hermes_constants import hermes_home_key
        hk = hermes_home_key()
        # The default profile key is the "default" home
        return hk is None or hk == "default"
    except Exception:
        return True


def get_account_secret(
    account_alias: str,
    secret_name: str,
    default: Optional[str] = None,
) -> Optional[str]:
    """Read an account-scoped secret.

    Account secrets are stored under keys like ``<provider>/<account>/...``.
    This function does NOT fall back to unscoped env vars — it resolves
    only under the active profile's scope.

    Args:
        account_alias: The local account alias (e.g. ``nous-global``).
        secret_name: The secret path (e.g. ``tiktok/nous-global/access-token``).
        default: Returned on miss.

    Returns:
        The secret value, or ``default``.
    """
    # Try host-scoped secret store under the account path
    val = _try_host_scoped_get(secret_name, default=None)
    if val is not None:
        return val

    # Also try the account-qualified env var pattern
    # e.g. TIKTOK_BUSINESS_ACCESS_TOKEN_NOUS_GLOBAL
    env_key = _account_env_key(secret_name, account_alias)
    val = os.environ.get(env_key)
    if val is not None and val != "":
        return val

    return default


def _account_env_key(secret_name: str, account_alias: str) -> str:
    """Convert a secret path into a flat env var name.

    e.g. ``tiktok/nous-global/access-token`` + ``nous-global``
    → ``TIKTOK_NOUS_GLOBAL_ACCESS_TOKEN``
    """
    # Replace non-alphanumeric with underscores
    parts = secret_name.replace("/", "_").replace("-", "_").replace(".", "_")
    return parts.upper()
