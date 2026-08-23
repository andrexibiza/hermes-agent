"""Typed, edge-specific child-process environment contracts.

The first migrated intents are the Bitwarden/1Password CLI and their
credential-free probes.  Other process classes are declared in the manifest
but remain inventory-only until their current behavior is mapped and migrated.

The central rule is that a child environment is constructed from a secret-free
OS baseline plus explicit grants.  It is never produced by copying the live
Hermes environment and subtracting names that currently look dangerous.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType

from tools.spawn_policy_manifest import SPAWN_POLICY, VAULT_GRANT_ENV, VAULT_GRANT_PREFIXES


class SpawnPolicyError(ValueError):
    """The requested process edge is not admitted by the canonical policy."""


class SpawnPrincipal(StrEnum):
    OPERATOR_INTERACTIVE = "operator_interactive"
    MODEL_AUTHORED = "model_authored"
    HERMES_CONTROL_PLANE = "hermes_control_plane"
    PLUGIN = "plugin"
    INSTALLER = "installer"
    EXTERNAL_TRUSTED_CLI = "external_trusted_cli"


class SpawnIntent(StrEnum):
    USER_INTERACTIVE_SHELL = "user_interactive_shell"
    MODEL_AUTHORED_COMMAND = "model_authored_command"
    MODEL_DRIVING_CLI = "model_driving_cli"
    HERMES_CONTROL_CHILD = "hermes_control_child"
    VAULT_CLI = "vault_cli"
    MCP_SERVER = "mcp_server"
    PLUGIN_SIDECAR = "plugin_sidecar"
    INSTALLER_OR_PROBE = "installer_or_probe"
    KANBAN_WORKER = "kanban_worker"
    DESKTOP_BACKEND = "desktop_backend"
    DESKTOP_MAINTENANCE = "desktop_maintenance"
    CHECKPOINT_GIT = "checkpoint_git"


class StdinPolicy(StrEnum):
    CLOSED = "closed"
    PIPE = "pipe"
    INHERIT = "inherit"


class DescendantPolicy(StrEnum):
    NO_AMBIENT_AUTHORITY = "no_ambient_authority"
    SAME_GRANTS = "same_grants"
    EXPLICIT_ONLY = "explicit_only"


def _frozen_environment(values: Mapping[str, str]) -> Mapping[str, str]:
    frozen: dict[str, str] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not key or "\x00" in key or "=" in key:
            raise SpawnPolicyError(f"invalid environment key: {key!r}")
        if not isinstance(value, str) or "\x00" in value:
            raise SpawnPolicyError(f"invalid environment value for {key!r}")
        frozen[key] = value
    return MappingProxyType(frozen)


@dataclass(frozen=True, slots=True)
class CapabilityGrant:
    """A bounded capability transported across one declared process edge."""

    grant_type: str
    environment: Mapping[str, str] = field(default_factory=dict)
    provenance: str = ""
    audience: SpawnIntent | None = None

    def __post_init__(self) -> None:
        if not self.grant_type or not isinstance(self.grant_type, str):
            raise SpawnPolicyError("capability grant_type is required")
        object.__setattr__(self, "environment", _frozen_environment(self.environment))


@dataclass(frozen=True, slots=True)
class SpawnSpec:
    """Immutable authorization and transport contract for one child edge."""

    executable: str
    principal: SpawnPrincipal
    intent: SpawnIntent
    target_profile: str | None = None
    grants: tuple[CapabilityGrant, ...] = ()
    stdin_policy: StdinPolicy = StdinPolicy.CLOSED
    descendant_policy: DescendantPolicy = DescendantPolicy.NO_AMBIENT_AUTHORITY
    source_callsite: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.executable, str) or not self.executable.strip():
            raise SpawnPolicyError("spawn executable is required")
        if "\x00" in self.executable:
            raise SpawnPolicyError("spawn executable contains NUL")
        if any(not isinstance(grant, CapabilityGrant) for grant in self.grants):
            raise SpawnPolicyError("all spawn grants must be CapabilityGrant values")


def normalized_policy_manifest() -> dict:
    """Return the canonical manifest as JSON-compatible data."""

    def normalize(value):
        if isinstance(value, Mapping):
            return {str(key): normalize(value[key]) for key in sorted(value)}
        if isinstance(value, (tuple, list)):
            return [normalize(item) for item in value]
        return value

    return normalize(SPAWN_POLICY)


def spawn_policy_hash() -> str:
    """SHA-256 over canonical manifest semantics, shared by generated consumers."""

    encoded = json.dumps(
        normalized_policy_manifest(), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _policy_for(spec: SpawnSpec) -> Mapping[str, object]:
    intents = SPAWN_POLICY.get("intents")
    if not isinstance(intents, Mapping):
        raise SpawnPolicyError("spawn policy manifest has no intents mapping")
    policy = intents.get(spec.intent.value)
    if not isinstance(policy, Mapping):
        raise SpawnPolicyError(f"unknown spawn intent: {spec.intent.value}")
    if not policy.get("implemented"):
        raise SpawnPolicyError(
            f"spawn intent {spec.intent.value!r} is inventory-only and has no admitted builder"
        )
    principals = tuple(policy.get("principals", ()))
    if spec.principal.value not in principals:
        raise SpawnPolicyError(
            f"principal {spec.principal.value!r} is not admitted for {spec.intent.value!r}"
        )
    expected_stdin = policy.get("stdin_policy")
    if expected_stdin != spec.stdin_policy.value:
        raise SpawnPolicyError(
            f"stdin policy {spec.stdin_policy.value!r} does not match {expected_stdin!r}"
        )
    expected_descendants = policy.get("descendant_policy")
    if expected_descendants != spec.descendant_policy.value:
        raise SpawnPolicyError(
            "descendant policy "
            f"{spec.descendant_policy.value!r} does not match {expected_descendants!r}"
        )
    return policy


def _source_value(source: Mapping[str, str], name: str) -> str | None:
    folded = name.casefold()
    matches = [(key, value) for key, value in source.items() if key.casefold() == folded]
    if not matches:
        return None
    distinct = {value for _, value in matches}
    if len(distinct) > 1:
        keys = ", ".join(key for key, _ in matches)
        raise SpawnPolicyError(f"ambiguous case-insensitive environment keys: {keys}")
    return matches[0][1]


def _allowed_grant_name(name: str, policy: Mapping[str, object]) -> bool:
    folded = name.casefold()
    exact = {str(item).casefold() for item in policy.get("grant_env", ())}
    if folded in exact:
        return True
    return any(
        folded.startswith(str(prefix).casefold())
        for prefix in policy.get("grant_prefixes", ())
    )


def _canonical_grant_name(name: str) -> str:
    folded = name.casefold()
    for candidate in VAULT_GRANT_ENV:
        if candidate.casefold() == folded:
            return candidate
    for prefix in VAULT_GRANT_PREFIXES:
        if folded.startswith(prefix.casefold()):
            return prefix + name[len(prefix) :]
    return name


def build_spawn_env(
    spec: SpawnSpec,
    *,
    source_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Construct the exact environment admitted for ``spec``.

    ``source_env`` supplies only secret-free baseline coordinates.  Authority
    values must arrive through an explicit :class:`CapabilityGrant`.
    """

    policy = _policy_for(spec)
    source = os.environ if source_env is None else source_env
    env: dict[str, str] = {}

    for name in policy.get("baseline_env", ()):
        value = _source_value(source, str(name))
        if value is not None:
            if not isinstance(value, str) or "\x00" in value:
                raise SpawnPolicyError(f"invalid baseline environment value for {name!r}")
            env[str(name)] = value

    static_env = policy.get("static_env", {})
    if not isinstance(static_env, Mapping):
        raise SpawnPolicyError("static_env must be a mapping")
    for key, value in static_env.items():
        env[str(key)] = str(value)

    admitted_grants = set(policy.get("grant_types", ()))
    for grant in spec.grants:
        if grant.grant_type not in admitted_grants:
            raise SpawnPolicyError(
                f"grant {grant.grant_type!r} is not admitted for {spec.intent.value!r}"
            )
        if grant.audience is not None and grant.audience != spec.intent:
            raise SpawnPolicyError(
                f"grant audience {grant.audience.value!r} does not match {spec.intent.value!r}"
            )
        for name, value in grant.environment.items():
            if not _allowed_grant_name(name, policy):
                raise SpawnPolicyError(
                    f"environment authority {name!r} is not admitted for {spec.intent.value!r}"
                )
            env[_canonical_grant_name(name)] = value

    return env


def vault_cli_spec(
    executable: str | Path,
    *,
    grant_env: Mapping[str, str] | None = None,
    provenance: str,
    target_profile: str | None = None,
    probe: bool = False,
) -> SpawnSpec:
    """Create the strict contract for a vault CLI invocation or probe."""

    grants: tuple[CapabilityGrant, ...] = ()
    if grant_env:
        if probe:
            raise SpawnPolicyError("credential-free probes cannot receive grants")
        grants = (
            CapabilityGrant(
                grant_type="vault_auth",
                environment=grant_env,
                provenance=provenance,
                audience=SpawnIntent.VAULT_CLI,
            ),
        )
    return SpawnSpec(
        executable=str(executable),
        principal=SpawnPrincipal.HERMES_CONTROL_PLANE,
        intent=(SpawnIntent.INSTALLER_OR_PROBE if probe else SpawnIntent.VAULT_CLI),
        target_profile=target_profile,
        grants=grants,
        stdin_policy=StdinPolicy.CLOSED,
        descendant_policy=DescendantPolicy.NO_AMBIENT_AUTHORITY,
        source_callsite=provenance,
    )


def build_vault_cli_env(
    *,
    executable: str | Path,
    source_env: Mapping[str, str] | None = None,
    grant_env: Mapping[str, str] | None = None,
    provenance: str,
    target_profile: str | None = None,
    probe: bool = False,
) -> dict[str, str]:
    """Construct a minimal Bitwarden/1Password child environment."""

    spec = vault_cli_spec(
        executable,
        grant_env=grant_env,
        provenance=provenance,
        target_profile=target_profile,
        probe=probe,
    )
    return build_spawn_env(spec, source_env=source_env)


def onepassword_grant_env(
    source_env: Mapping[str, str], *, token_value: str = ""
) -> dict[str, str]:
    """Select only the 1Password authentication capabilities from ``source_env``."""

    result: dict[str, str] = {}
    for name in (
        "OP_ACCOUNT",
        "OP_CONNECT_HOST",
        "OP_CONNECT_TOKEN",
        "OP_LOAD_DESKTOP_APP_SETTINGS",
        "OP_CACHE",
    ):
        value = _source_value(source_env, name)
        if value is not None:
            result[name] = value
    for key, value in source_env.items():
        if key.casefold().startswith("op_session_"):
            result[_canonical_grant_name(key)] = value
    if token_value:
        result["OP_SERVICE_ACCOUNT_TOKEN"] = token_value
    return result


def executable_is_absolute(path: str | Path) -> bool:
    """Cross-platform absolute-path predicate used by stricter future intents."""

    text = str(path)
    return os.path.isabs(text) or ntpath.isabs(text)
