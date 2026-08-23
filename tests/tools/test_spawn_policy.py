from __future__ import annotations

import json

import pytest

from tools.spawn_policy import (
    CapabilityGrant,
    DescendantPolicy,
    SpawnIntent,
    SpawnPolicyError,
    SpawnPrincipal,
    SpawnSpec,
    StdinPolicy,
    bitwarden_capability_envs,
    build_spawn_env,
    build_vault_cli_env,
    normalized_policy_manifest,
    onepassword_capability_envs,
    spawn_policy_hash,
)


def test_manifest_names_every_phase_g_intent_and_hashes_canonically():
    manifest = normalized_policy_manifest()
    assert set(manifest["intents"]) == {intent.value for intent in SpawnIntent}
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    assert len(spawn_policy_hash()) == 64
    assert spawn_policy_hash() == __import__("hashlib").sha256(encoded.encode()).hexdigest()


def test_vault_env_is_baseline_plus_explicit_grants_only():
    source = {
        "PATH": "/usr/bin",
        "HOME": "/home/user",
        "HTTPS_PROXY": "https://proxy-user:proxy-pass@proxy.example",
        "SSL_CERT_FILE": "/etc/ca.pem",
        "OPENAI_API_KEY": "provider-secret",
        "HERMES_KANBAN_TASK": "parent-task",
        "_HERMES_GATEWAY": "1",
        "NODE_OPTIONS": "--require attacker.js",
        "BWS_ACCESS_TOKEN": "ambient-vault-token",
    }
    auth, route = bitwarden_capability_envs(source, access_token="edge-token")
    env = build_vault_cli_env(
        executable="/usr/bin/bws",
        source_env=source,
        auth_env=auth,
        route_env=route,
        provenance="test",
    )
    assert env == {
        "PATH": "/usr/bin",
        "HOME": "/home/user",
        "NO_COLOR": "1",
        "BWS_ACCESS_TOKEN": "edge-token",
        "HTTPS_PROXY": "https://proxy-user:proxy-pass@proxy.example",
        "SSL_CERT_FILE": "/etc/ca.pem",
    }


def test_route_authority_is_not_ambient_without_an_explicit_route_grant():
    env = build_vault_cli_env(
        executable="/usr/bin/bws",
        source_env={"PATH": "/usr/bin", "HTTPS_PROXY": "https://secret@proxy"},
        auth_env={"BWS_ACCESS_TOKEN": "token"},
        provenance="test",
    )
    assert "HTTPS_PROXY" not in env


def test_probe_cannot_receive_any_grant():
    with pytest.raises(SpawnPolicyError, match="probes cannot receive grants"):
        build_vault_cli_env(
            executable="/usr/bin/op",
            source_env={},
            auth_env={"OP_SERVICE_ACCOUNT_TOKEN": "secret"},
            provenance="test",
            probe=True,
        )


def test_auth_grant_cannot_smuggle_route_or_provider_authority():
    for name in ("HTTPS_PROXY", "OPENAI_API_KEY"):
        with pytest.raises(SpawnPolicyError, match=name):
            build_vault_cli_env(
                executable="/usr/bin/op",
                source_env={},
                auth_env={name: "secret"},
                provenance="test",
            )


def test_onepassword_grants_are_split_by_capability_and_prefix_aware():
    source = {
        "OP_ACCOUNT": "team",
        "OP_CONNECT_HOST": "https://connect.example",
        "OP_CONNECT_TOKEN": "connect-secret",
        "OP_LOAD_DESKTOP_APP_SETTINGS": "false",
        "OP_CACHE": "false",
        "OP_SESSION_work": "session-secret",
        "HTTPS_PROXY": "https://proxy.example",
        "OPENAI_API_KEY": "provider-secret",
    }
    auth, route = onepassword_capability_envs(source, token_value="service-token")
    assert auth == {
        "OP_CONNECT_TOKEN": "connect-secret",
        "OP_SESSION_work": "session-secret",
        "OP_SERVICE_ACCOUNT_TOKEN": "service-token",
    }
    assert route == {
        "HTTPS_PROXY": "https://proxy.example",
        "OP_ACCOUNT": "team",
        "OP_CONNECT_HOST": "https://connect.example",
        "OP_LOAD_DESKTOP_APP_SETTINGS": "false",
        "OP_CACHE": "false",
    }


def test_windows_environment_lookup_is_case_insensitive_and_canonicalized():
    env = build_vault_cli_env(
        executable=r"C:\Program Files\Bitwarden\bws.exe",
        source_env={"Path": r"C:\Windows", "systemroot": r"C:\Windows"},
        auth_env={"bws_access_token": "token"},
        provenance="test",
    )
    assert env["PATH"] == r"C:\Windows"
    assert env["SystemRoot"] == r"C:\Windows"
    assert env["BWS_ACCESS_TOKEN"] == "token"


def test_ambiguous_case_insensitive_source_fails_closed():
    with pytest.raises(SpawnPolicyError, match="ambiguous"):
        build_vault_cli_env(
            executable="op",
            source_env={"PATH": "/a", "Path": "/b"},
            provenance="test",
            probe=True,
        )


def test_inventory_only_intent_cannot_use_generic_builder():
    spec = SpawnSpec(
        executable="bash",
        principal=SpawnPrincipal.MODEL_AUTHORED,
        intent=SpawnIntent.MODEL_AUTHORED_COMMAND,
        stdin_policy=StdinPolicy.CLOSED,
        descendant_policy=DescendantPolicy.NO_AMBIENT_AUTHORITY,
    )
    with pytest.raises(SpawnPolicyError, match="inventory-only"):
        build_spawn_env(spec, source_env={})


def test_grants_are_immutable_and_reject_nul_values():
    grant = CapabilityGrant("vault_auth", {"BWS_ACCESS_TOKEN": "token"})
    with pytest.raises(TypeError):
        grant.environment["BWS_ACCESS_TOKEN"] = "changed"  # type: ignore[index]
    with pytest.raises(SpawnPolicyError, match="invalid environment value"):
        CapabilityGrant("vault_auth", {"BWS_ACCESS_TOKEN": "bad\x00token"})
