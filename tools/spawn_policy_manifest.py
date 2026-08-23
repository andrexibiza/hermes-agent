"""Canonical child-process intent and environment policy.

This module is deliberately data-only. Python runtime code consumes it now;
a generated TypeScript projection can consume the same normalized mapping
without maintaining a second credential/authority policy by hand.
"""

from __future__ import annotations

POLICY_VERSION = 1

# Secret-free operating-system substrate required by trusted external CLIs.
# Values are copied from the selected source environment only when present.
VAULT_OS_BASELINE_ENV = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "USERNAME",
    "USERPROFILE",
    "APPDATA",
    "LOCALAPPDATA",
    "SystemRoot",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TMPDIR",
    "TMP",
    "TEMP",
    "XDG_CONFIG_HOME",
    "XDG_RUNTIME_DIR",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
)

# Authentication grants are never inferred from the live environment by the
# generic builder. Callers must select and pass them deliberately.
VAULT_AUTH_ENV = (
    "BWS_ACCESS_TOKEN",
    "OP_SERVICE_ACCOUNT_TOKEN",
    "OP_CONNECT_TOKEN",
)
VAULT_AUTH_PREFIXES = ("OP_SESSION_",)

# Network/account routing can itself contain authority (for example a proxy
# URL with embedded credentials), so it is a separate explicit grant rather
# than part of the baseline.
VAULT_ROUTE_ENV = (
    "BWS_SERVER_URL",
    "OP_ACCOUNT",
    "OP_CONNECT_HOST",
    "OP_LOAD_DESKTOP_APP_SETTINGS",
    "OP_CACHE",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
)

# Every intended repository process class is named here even before migration.
# ``implemented`` means the generic builder currently owns the environment
# contract for that intent; false entries are inventory/admission targets and
# must not be used to create a child through the generic builder yet.
SPAWN_POLICY = {
    "version": POLICY_VERSION,
    "intents": {
        "user_interactive_shell": {"implemented": False},
        "model_authored_command": {"implemented": False},
        "model_driving_cli": {"implemented": False},
        "hermes_control_child": {"implemented": False},
        "vault_cli": {
            "implemented": True,
            "principals": ("hermes_control_plane",),
            "baseline_env": VAULT_OS_BASELINE_ENV,
            "grants": {
                "vault_auth": {
                    "env": VAULT_AUTH_ENV,
                    "prefixes": VAULT_AUTH_PREFIXES,
                },
                "vault_route": {
                    "env": VAULT_ROUTE_ENV,
                    "prefixes": (),
                },
            },
            "static_env": {"NO_COLOR": "1"},
            "stdin_policy": "closed",
            "descendant_policy": "no_ambient_authority",
        },
        "mcp_server": {"implemented": False},
        "plugin_sidecar": {"implemented": False},
        "installer_or_probe": {
            "implemented": True,
            "principals": ("hermes_control_plane",),
            "baseline_env": VAULT_OS_BASELINE_ENV,
            "grants": {},
            "static_env": {"NO_COLOR": "1"},
            "stdin_policy": "closed",
            "descendant_policy": "no_ambient_authority",
        },
        "kanban_worker": {"implemented": False},
        "desktop_backend": {"implemented": False},
        "desktop_maintenance": {"implemented": False},
        "checkpoint_git": {"implemented": False},
    },
}
