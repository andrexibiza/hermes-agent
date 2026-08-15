"""Central child-process credential boundary for Hermes Agent.

This module is intentionally dependency-light so every subprocess surface can
share the same classifier without importing the terminal backend.  It filters
ambient environment mappings before process creation and supports explicit,
narrow capability restoration by callers that genuinely need it.
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Callable

_FORCE_PREFIX = "_HERMES_FORCE_"
_FORWARD_PREFIXES = ("APPTAINERENV_", "SINGULARITYENV_")

# Tier-1 credentials never cross an untrusted child boundary, even when a
# caller opts into provider credential inheritance.
_ALWAYS_STRIP = frozenset({
    "BWS_ACCESS_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GITHUB_APP_ID",
    "GITHUB_APP_PRIVATE_KEY_PATH",
    "GITHUB_APP_INSTALLATION_ID",
    "HERMES_DASHBOARD_SESSION_TOKEN",
    "GATEWAY_ALLOWED_USERS",
    "GATEWAY_ALLOW_ALL_USERS",
    "GATEWAY_RELAY_ID",
    "GATEWAY_RELAY_SECRET",
    "GATEWAY_RELAY_DELIVERY_KEY",
    "TELEGRAM_BOT_TOKEN",
    "DISCORD_BOT_TOKEN",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_SIGNING_SECRET",
    "EMAIL_PASSWORD",
    "HASS_TOKEN",
    "MODAL_TOKEN_ID",
    "MODAL_TOKEN_SECRET",
    "DAYTONA_API_KEY",
    "AZURE_CLIENT_SECRET",
    "AZURE_FEDERATED_TOKEN_FILE",
    # Profile-scoped Tlon connection identity.  Presence can auto-enable a
    # platform in a child profile even when that profile has no Tlon config.
    "TLON_SHIP_URL",
    "TLON_SHIP_NAME",
    "TLON_SHIP_CODE",
})

# General operator capabilities that SECURITY.md explicitly permits the local
# operator shell to inherit.  They are *not* automatically restored for other
# process classes; callers must pass them in compatibility_keep.
_OPERATOR_COMPAT = frozenset({
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
    "AWS_CONFIG_FILE",
    "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_DEFAULT_REGION",
    "AWS_REGION",
    "AWS_ROLE_ARN",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "SSH_AUTH_SOCK",
    "GPG_AGENT_INFO",
})

# Exact benign variables whose names contain otherwise-sensitive words.
_BENIGN_EXACT = frozenset({
    "PWD", "OLDPWD", "PATH", "PATHEXT", "AUTHORS", "PASSWORD_STORE_DIR",
    "PASSENGER_APP_ENV", "COMPASS_DIR", "BYPASS_CACHE",
})

# Credential-bearing connection strings.  This intentionally matches only
# URI userinfo with a non-empty password before @, not arbitrary URLs.
_CREDENTIAL_URI = re.compile(
    r"(?i)^[a-z][a-z0-9+.-]*://[^/@\s:]+:[^/@\s]+@[^\s]+$"
)

# Credential tokens must occur as a full env-name segment.  This catches
# DB_PASS / APP_PWD / BWS_CRED while avoiding ordinary words containing those
# letters (COMPASS, BYPASS, AUTHORITY, MONKEY, etc.).
_SECRET_SEGMENTS = frozenset({
    "APIKEY", "API_KEY", "KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD",
    "PASS", "PWD", "CREDENTIAL", "CREDENTIALS", "CRED", "CREDS", "BEARER",
    "WEBHOOK", "DSN", "PRIVATEKEY", "PRIVATE_KEY",
})


def destination_key(key: str) -> str:
    """Normalize wrapper/force destinations before classification."""
    current = str(key)
    while True:
        upper = current.upper()
        if upper.startswith(_FORCE_PREFIX):
            current = current[len(_FORCE_PREFIX):]
            continue
        matched = False
        for prefix in _FORWARD_PREFIXES:
            if upper.startswith(prefix):
                current = current[len(prefix):]
                matched = True
                break
        if not matched:
            return current


def _segments(key: str) -> tuple[str, ...]:
    upper = destination_key(key).upper()
    parts = tuple(p for p in re.split(r"[^A-Z0-9]+", upper) if p)
    return parts


def is_generic_secret_name(key: str) -> bool:
    """Return True for credential-shaped environment names.

    The classifier is case-insensitive and wrapper-aware.  It deliberately
    avoids broad substring rules that would reject COMPASS/BYPASS/etc.
    """
    dest = destination_key(key).upper()
    if dest in _BENIGN_EXACT:
        return False
    if dest in _ALWAYS_STRIP:
        return True
    if dest.startswith("AUXILIARY_") and (
        dest.endswith("_API_KEY") or dest.endswith("_BASE_URL")
    ):
        return True
    if dest.startswith("GATEWAY_RELAY_") and (
        dest.endswith("_SECRET") or dest.endswith("_KEY") or dest.endswith("_TOKEN")
    ):
        return True
    parts = _segments(dest)
    joined_pairs = {f"{a}_{b}" for a, b in zip(parts, parts[1:])}
    if any(p in _SECRET_SEGMENTS for p in parts):
        return True
    if joined_pairs & _SECRET_SEGMENTS:
        return True
    return False


def value_looks_secret(value: object, *, applied_secret_values: Iterable[str] = ()) -> bool:
    """Classify a value by provenance or embedded connection-string auth."""
    if value is None:
        return False
    text = str(value)
    if not text:
        return False
    applied = {str(v) for v in applied_secret_values if v not in (None, "")}
    if text in applied:
        return True
    return bool(_CREDENTIAL_URI.match(text))


@dataclass(frozen=True)
class FilterDecision:
    key: str
    destination: str
    allowed: bool
    reason: str


def classify(
    key: str,
    value: object,
    *,
    provider_blocklist: Iterable[str] = (),
    applied_secret_values: Iterable[str] = (),
    passthrough: Iterable[str] = (),
    inherit_provider_credentials: bool = False,
    compatibility_keep: Iterable[str] = (),
) -> FilterDecision:
    """Classify one environment entry at the child boundary."""
    dest = destination_key(key)
    upper = dest.upper()
    provider = {str(v).upper() for v in provider_blocklist}
    pass_names = {str(v).upper() for v in passthrough}
    compat = {str(v).upper() for v in compatibility_keep}

    if upper in _ALWAYS_STRIP:
        return FilterDecision(key, dest, False, "tier1")
    # An explicit force-prefix opt-in overrides the hard-deny blocks below
    # (provider-credential, generic-secret-name, secret-provenance) but NOT
    # Tier-1 always-strip — that would let callers leak gateway/GitHub tokens.
    # The destination is already stripped of _HERMES_FORCE_ by destination_key().
    is_force = str(key).upper().startswith(_FORCE_PREFIX)
    if is_force and upper not in _ALWAYS_STRIP:
        return FilterDecision(key, dest, True, "force_opt_in")
    if upper in provider and inherit_provider_credentials:
        return FilterDecision(key, dest, True, "explicit-provider-capability")
    if upper in provider:
        return FilterDecision(key, dest, False, "provider-credential")
    # An explicit terminal/skill passthrough is a capability contract.  It may
    # carry a generic credential or an externally-applied secret, but never a
    # Tier-1 or provider credential (those were decided above).  The resolver
    # is applied later so multiplex callers receive the active profile's value
    # rather than whatever happens to be in process-global os.environ.
    if upper in pass_names:
        return FilterDecision(key, dest, True, "explicit-passthrough")
    if upper in compat:
        return FilterDecision(key, dest, True, "compatibility-capability")
    if value_looks_secret(value, applied_secret_values=applied_secret_values):
        return FilterDecision(key, dest, False, "secret-provenance-or-embedded-auth")
    if is_generic_secret_name(key):
        return FilterDecision(key, dest, False, "credential-shaped")
    return FilterDecision(key, dest, True, "benign")


def filter_child_env(
    base: Mapping[str, object] | None = None,
    *,
    provider_blocklist: Iterable[str] = (),
    applied_secret_values: Iterable[str] = (),
    passthrough: Iterable[str] = (),
    passthrough_resolver: Callable[[str, object], object | None] | None = None,
    inherit_provider_credentials: bool = False,
    compatibility_keep: Iterable[str] = (),
    extra: Mapping[str, object] | None = None,
) -> dict[str, str]:
    """Build a sanitized child environment.

    ``extra`` is classified by destination key exactly like ``base``; adding a
    value later therefore cannot bypass the boundary.  If a passthrough
    resolver is provided, it is consulted only after the name has survived the
    hard deny rules.
    """
    source: dict[str, object] = dict(os.environ if base is None else base)
    if extra:
        source.update(extra)

    passthrough_upper = {str(v).upper() for v in passthrough}
    out: dict[str, str] = {}
    for key, value in source.items():
        decision = classify(
            key,
            value,
            provider_blocklist=provider_blocklist,
            applied_secret_values=applied_secret_values,
            passthrough=passthrough,
            inherit_provider_credentials=inherit_provider_credentials,
            compatibility_keep=compatibility_keep,
        )
        if not decision.allowed:
            continue
        resolved = value
        if decision.destination.upper() in passthrough_upper and passthrough_resolver:
            resolved = passthrough_resolver(decision.destination, value)
        if resolved is None:
            continue
        out[decision.destination] = str(resolved)
    return out


def minimal_child_env(
    *,
    base: Mapping[str, object] | None = None,
    allow: Iterable[str] = (),
    extra: Mapping[str, object] | None = None,
) -> dict[str, str]:
    """Build an allowlist-only environment for privileged helpers."""
    src = dict(os.environ if base is None else base)
    wanted = {str(v).upper() for v in allow}
    out = {k: str(v) for k, v in src.items() if k.upper() in wanted and v is not None}
    if extra:
        for key, value in extra.items():
            if value is not None:
                out[str(key)] = str(value)
    return out


DEFAULT_EXECUTABLE_ALLOWLIST = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
    "TMPDIR", "HOME", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "LANG",
    "LC_ALL", "TERM", "COLORTERM", "PYTHONUTF8",
})

OPERATOR_COMPATIBILITY_KEYS = _OPERATOR_COMPAT
ALWAYS_STRIP_KEYS = _ALWAYS_STRIP
