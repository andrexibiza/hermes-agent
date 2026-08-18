"""Persistent MCP tool-schema cache for lazy server startup.

Stores per-server tool manifests on disk so Hermes can register MCP tools
into the agent snapshot without spawning the stdio child process at idle
dashboard startup. Cache entries are keyed by server name + a fingerprint
of the connection config (command/args/url/tools filters, protocol era,
auth/identity context, headers/env).

Schema identity (#88698, R3): entries are stamped with
``CACHE_SCHEMA_VERSION`` and partitioned by ``server_name::fingerprint``,
so a context that differs in any schema-affecting dimension (protocol era,
auth context, headers, env) can never read another context's entries.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_CACHE_FILENAME = "mcp_schema_cache.json"
_cache_lock = threading.Lock()

# Epoch for the on-disk entry schema. Any future semantic change to the
# entry schema / TTL semantics / scope enforcement MUST bump this constant
# in the same change: it is folded into every fingerprint (so every old
# entry misses on next read) and stamped onto every entry (hard read gate).
CACHE_SCHEMA_VERSION = 2

# Upper bound on how long a cached tool manifest may be trusted without a
# live reconfirmation, regardless of what the server advertised (SDK parity:
# mcp/client/caching.py clamps the response cache to 24 h). Applied on write
# AND read (defense in depth against hand-edited entries).
MAX_TTL_MS = 24 * 60 * 60 * 1000


def _cache_path() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "cache" / _CACHE_FILENAME


def _h(value: Any) -> str:
    """Hash a scalar value for fingerprint/digest payloads (never plaintext)."""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _hash_leaves(value: Any) -> Any:
    """Recursively replace every scalar leaf with its sha256 hash.

    Used for secret-bearing config surfaces (oauth, env, headers): the
    structure (key names) stays readable, values never appear in plaintext.
    """
    if isinstance(value, dict):
        return {str(k): _hash_leaves(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_hash_leaves(v) for v in value]
    return _h(value)


def _resolve_protocol_era() -> str:
    """Return the handshake protocol era this codebase actually speaks.

    Mirrors tools/mcp_tool.py's header seeding: the MCP-Protocol-Version
    header is seeded from ``LATEST_HANDSHAKE_VERSION`` (2025-11-25), so that
    constant is the era string used in the fingerprint. Falls back to the
    installed SDK package version, then ``"unknown"`` — the value must be
    stable within a process (the SDK version cannot change mid-process).
    """
    try:
        from mcp.client.session import LATEST_HANDSHAKE_VERSION

        return str(LATEST_HANDSHAKE_VERSION)
    except Exception:
        pass
    try:
        from importlib.metadata import version as _pkg_version

        return _pkg_version("mcp") or "unknown"
    except Exception:
        return "unknown"


def _resolve_identity_header_term(config: dict) -> Optional[list]:
    """Resolve the ``identity_header`` config to ``[name.lower(), hash(value)]``.

    Same pure-config resolver semantics as
    ``tools.mcp_tool._resolve_identity_header`` (static value / active
    profile name), reimplemented locally to avoid a circular import: the
    profile name is a principal identifier, not a secret, so it is hashed
    here only for payload uniformity. Invalid config → omitted sub-term.
    """
    raw = config.get("identity_header")
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        return None
    value_from = (raw.get("value_from") or "static").strip().lower()
    if value_from == "static":
        value = raw.get("value")
        if not isinstance(value, str) or not value.strip():
            return None
        return [name.strip().lower(), _h(value)]
    if value_from == "profile":
        try:
            from hermes_cli import profiles as _profiles

            value = _profiles.get_active_profile_name()
        except Exception:
            return None
        if not value:
            return None
        return [name.strip().lower(), _h(value)]
    return None


def _auth_context_term(config: dict) -> dict:
    """Build the auth-context fingerprint sub-terms (never token bytes)."""
    term: dict = {}
    auth_type = (config.get("auth") or "").lower().strip()
    if auth_type:
        term["auth_type"] = auth_type
    oauth = config.get("oauth")
    if isinstance(oauth, dict) and oauth:
        term["oauth"] = _hash_leaves(oauth)
    # Token-file rotation discriminator: a refreshed token changes the file's
    # mtime → new partition → safe re-probe, with no secret persisted.
    for key in ("auth_token_file", "token_file"):
        token_path = config.get(key)
        if not isinstance(token_path, (str, Path)) or not str(token_path):
            continue
        try:
            stat = Path(str(token_path)).stat()
            term["token_file"] = [str(token_path), stat.st_mtime_ns]
        except OSError:
            continue
        break
    identity_term = _resolve_identity_header_term(config)
    if identity_term is not None:
        term["identity_header"] = identity_term
    term["tls"] = [config.get("ssl_verify", True), config.get("client_cert")]
    return term


def config_digest(config: dict) -> str:
    """Hash the ENTIRE config dict (every key), value-hashed, canonical JSON.

    Complements ``config_fingerprint``: the fingerprint is the
    schema-affecting partition key (non-schema-affecting keys such as
    ``timeout``/``enabled``/``lazy`` stay out of it), while this digest
    invalidates the entry on ANY config edit — an operator editing
    ``timeout`` expects the cached manifest to be re-verified too. Values
    are hashed so no secret plaintext is added to the cache file.
    """
    hashed = _hash_leaves(config)
    raw = json.dumps(hashed, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def config_fingerprint(config: dict) -> str:
    """Stable hash of the connection-defining parts of an MCP server config.

    The fingerprint is the schema-affecting partition key: connection
    shape (command/args/url/transport), tool filters, protocol era + pin,
    headers, env, redirect-header strictness, and the auth/identity context
    (so principal A can never read principal B's entries). Secret-bearing
    values enter only as sha256 hashes; non-schema-affecting keys
    (``timeout``, ``enabled``, ``lazy``) stay out — they are covered by
    :func:`config_digest` at the entry level instead.
    """
    tools_filter = config.get("tools") or {}
    payload = {
        # existing (unchanged)
        "command": config.get("command"),
        "args": config.get("args") or [],
        "url": config.get("url"),
        "transport": config.get("transport"),
        "tools_include": sorted(tools_filter.get("include") or []),
        "tools_exclude": sorted(tools_filter.get("exclude") or []),
        # C1 epoch: bumping CACHE_SCHEMA_VERSION atomically changes every
        # fingerprint, so every existing entry misses on next read.
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        # new context terms (#88698 R3)
        "headers": sorted(
            (k.lower(), _h(v))
            for k, v in (config.get("headers") or {}).items()
        ),
        "env": sorted(
            (k, _h(v)) for k, v in (config.get("env") or {}).items()
        ),
        "strict_redirect_headers": bool(config.get("strict_redirect_headers")),
        "auth_context": _auth_context_term(config),
        "protocol_era": _resolve_protocol_era(),
        "protocol_pin": config.get("protocol_version"),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _entry_key(server_name: str, fingerprint: str) -> str:
    """Partition key: one server may hold several context partitions."""
    return f"{server_name}::{fingerprint}"


def _load_all() -> Dict[str, Any]:
    path = _cache_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.debug("Could not read MCP schema cache %s: %s", path, exc)
        return {}


def _save_all(data: Dict[str, Any]) -> None:
    from utils import atomic_json_write

    # Cache dir + 0o600: sibling precedent in tools/registry.py
    # _save_discovery_cache; the cache file is trusted input on the lazy
    # registration path, so keep it user-only.
    atomic_json_write(_cache_path(), data, mode=0o600)


def get_cached_entry(
    server_name: str,
    fingerprint: str,
    *,
    config_digest: Optional[str] = None,
) -> Optional[dict]:
    """Return cached entry when identity holds (and TTL holds), else None.

    Fail-closed read gate order (first miss wins):

    1. partition-key lookup (``server_name::fingerprint``);
    2. ``epoch`` must equal :data:`CACHE_SCHEMA_VERSION`;
    3. stored ``fingerprint`` must equal the recomputed one;
    4. when *config_digest* is given, it must match the stored digest (a
       ``config_digest=None`` read skips this gate for back-compat);
    5. TTL check with :data:`MAX_TTL_MS` clamping (an entry is never trusted
       past 24 h no matter what the server advertised or a hand-edit wrote).

    MCP 2026-07-28 (SEP-2549): ``tools/list`` results carry ``ttlMs`` as a
    freshness hint. When the live discovery path recorded one, an entry
    older than its TTL is treated as a miss so the next startup re-probes
    the server instead of serving a stale manifest forever. Entries without
    a recorded TTL (pre-2026 servers) keep the old never-expires behavior
    (bounded only by :data:`MAX_TTL_MS` once a TTL exists).

    ``cacheScope`` is recorded for diagnostics only; enforcement is
    structural — the fingerprint partitions by auth/identity context, so a
    ``private``-scoped entry written under one principal is unreachable
    from another.
    """
    with _cache_lock:
        entry = _load_all().get(_entry_key(server_name, fingerprint))
    if not isinstance(entry, dict):
        return None
    if entry.get("epoch") != CACHE_SCHEMA_VERSION:
        return None
    if entry.get("fingerprint") != fingerprint:
        return None
    if config_digest is not None and entry.get("config_digest") != config_digest:
        return None
    ttl_ms = entry.get("ttl_ms")
    written_at = entry.get("written_at")
    if isinstance(ttl_ms, (int, float)) and isinstance(written_at, (int, float)):
        effective_ttl = min(float(ttl_ms), MAX_TTL_MS)
        if (time.time() - written_at) * 1000.0 >= effective_ttl:
            return None
    return entry


def has_cached_entry(server_name: str, fingerprint: str) -> bool:
    return get_cached_entry(server_name, fingerprint) is not None


def write_cache_entry(
    server_name: str,
    fingerprint: str,
    *,
    config_digest: Optional[str] = None,
    protocol_era: Optional[str] = None,
    negotiated_era: Optional[str] = None,
    tools: List[dict],
    utility_tools: Optional[List[dict]] = None,
    ttl_ms: Optional[float] = None,
    cache_scope: Optional[str] = None,
) -> None:
    """Persist tool schemas after a successful live connect.

    ``ttl_ms``/``cache_scope`` are the SEP-2549 hints from the server's
    ``tools/list`` result (2026-07-28 servers); ``ttl_ms`` is clamped to
    :data:`MAX_TTL_MS`. ``written_at`` anchors TTL expiry in
    :func:`get_cached_entry`. ``config_digest`` and ``protocol_era`` are
    identity fields: ``protocol_era`` is the client-side era the manifest
    was fetched under (diagnostic + partition documentation), and
    ``config_digest`` lets any config edit invalidate the entry on read.
    """
    entry = {
        "epoch": CACHE_SCHEMA_VERSION,
        "fingerprint": fingerprint,
        "config_digest": config_digest,
        "protocol_era": protocol_era,
        "tools": tools,
        "utility_tools": utility_tools or [],
    }
    if negotiated_era is not None:
        entry["negotiated_era"] = negotiated_era
    if isinstance(ttl_ms, (int, float)):
        entry["ttl_ms"] = min(float(ttl_ms), MAX_TTL_MS)
        entry["written_at"] = time.time()
    if cache_scope:
        entry["cache_scope"] = cache_scope
    key = _entry_key(server_name, fingerprint)
    with _cache_lock:
        data = _load_all()
        # Write-through fires on every registration (reconnects,
        # list_changed refreshes); skip the load-all+rewrite churn when the
        # entry is byte-identical to what is already on disk. TTL'd entries
        # always rewrite: written_at must advance or the entry would expire
        # at its ORIGINAL write time no matter how many live reconnects
        # confirmed it since.
        if "written_at" not in entry and data.get(key) == entry:
            return
        data[key] = entry
        _save_all(data)


def clear_cache_entry(server_name: str) -> None:
    """Clear every context partition recorded for *server_name*."""
    prefix = f"{server_name}::"
    with _cache_lock:
        data = _load_all()
        stale = [key for key in data if key.startswith(prefix)]
        if not stale:
            return
        for key in stale:
            del data[key]
        _save_all(data)


def tools_from_cache_entry(entry: dict) -> List[dict]:
    """Return cached MCP tool dicts (name, description, inputSchema)."""
    tools = entry.get("tools")
    return list(tools) if isinstance(tools, list) else []


def utility_tools_from_cache_entry(entry: dict) -> List[dict]:
    util = entry.get("utility_tools")
    return list(util) if isinstance(util, list) else []
