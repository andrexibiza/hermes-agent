"""Generation-bound static manifests for truly lazy MCP servers.

The manifest deliberately contains only tool routing metadata and schemas. MCP
transport configuration (commands, URLs, headers, environment variables, and
credentials) stays in config.yaml and is never serialized here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from hermes_constants import get_hermes_home

CATALOG_FORMAT_VERSION = 1
MAX_CATALOG_BYTES = 4 * 1024 * 1024
_TOP_LEVEL_FIELDS = frozenset({"format_version", "server", "generation", "created_at", "tools"})
_TOOL_REQUIRED_FIELDS = frozenset({"registry_name", "remote_name", "kind", "schema"})
_TOOL_FIELDS = _TOOL_REQUIRED_FIELDS | {"metadata"}
_SCHEMA_FIELDS = frozenset({"name", "description", "parameters"})
_METADATA_FIELDS = frozenset({"output_schema", "annotations"})
_VALID_KINDS = frozenset({"tool", "utility"})


class CatalogValidationError(ValueError):
    """Raised when a static MCP catalog fails structural or digest checks."""


class CatalogNotFoundError(FileNotFoundError):
    """Raised when a lazy MCP server has no bootstrapped catalog."""


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CatalogValidationError(f"catalog is not canonical JSON: {exc}") from exc


def _generation(server: str, tools: list[dict[str, Any]]) -> str:
    digest_payload = {
        "format_version": CATALOG_FORMAT_VERSION,
        "server": server,
        "tools": tools,
    }
    return f"sha256:{hashlib.sha256(_canonical_json(digest_payload)).hexdigest()}"


def _validated_tools(entries: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_entry in enumerate(entries):
        if not isinstance(raw_entry, dict):
            raise CatalogValidationError(f"tool entry {index} must be an object")
        unknown = set(raw_entry) - _TOOL_FIELDS
        missing = _TOOL_REQUIRED_FIELDS - set(raw_entry)
        if unknown:
            raise CatalogValidationError(
                f"tool entry {index} has unknown fields: {', '.join(sorted(unknown))}"
            )
        if missing:
            raise CatalogValidationError(
                f"tool entry {index} is missing fields: {', '.join(sorted(missing))}"
            )

        registry_name = raw_entry["registry_name"]
        remote_name = raw_entry["remote_name"]
        kind = raw_entry["kind"]
        schema = raw_entry["schema"]
        metadata = raw_entry.get("metadata", {})
        if not isinstance(registry_name, str) or not registry_name:
            raise CatalogValidationError(f"tool entry {index} has invalid registry_name")
        if registry_name in seen:
            raise CatalogValidationError(f"duplicate tool name in catalog: {registry_name}")
        seen.add(registry_name)
        if not isinstance(remote_name, str) or not remote_name:
            raise CatalogValidationError(f"tool entry {index} has invalid remote_name")
        if kind not in _VALID_KINDS:
            raise CatalogValidationError(f"tool entry {index} has invalid kind: {kind!r}")
        if not isinstance(schema, dict):
            raise CatalogValidationError(f"tool entry {index} schema must be an object")
        schema_unknown = set(schema) - _SCHEMA_FIELDS
        schema_missing = _SCHEMA_FIELDS - set(schema)
        if schema_unknown or schema_missing:
            details = []
            if schema_unknown:
                details.append(f"unknown fields: {', '.join(sorted(schema_unknown))}")
            if schema_missing:
                details.append(f"missing fields: {', '.join(sorted(schema_missing))}")
            raise CatalogValidationError(
                f"tool entry {index} schema has {'; '.join(details)}"
            )
        if schema["name"] != registry_name:
            raise CatalogValidationError(
                f"tool entry {index} schema name does not match registry_name"
            )
        if not isinstance(schema["description"], str):
            raise CatalogValidationError(f"tool entry {index} description must be a string")
        if not isinstance(schema["parameters"], dict):
            raise CatalogValidationError(f"tool entry {index} parameters must be an object")
        if not isinstance(metadata, dict):
            raise CatalogValidationError(f"tool entry {index} metadata must be an object")
        metadata_unknown = set(metadata) - _METADATA_FIELDS
        if metadata_unknown:
            raise CatalogValidationError(
                f"tool entry {index} metadata has unknown fields: "
                f"{', '.join(sorted(metadata_unknown))}"
            )
        for field, value in metadata.items():
            if not isinstance(value, dict):
                raise CatalogValidationError(
                    f"tool entry {index} metadata.{field} must be an object"
                )

        normalized.append(
            {
                "registry_name": registry_name,
                "remote_name": remote_name,
                "kind": kind,
                "schema": {
                    "name": registry_name,
                    "description": schema["description"],
                    "parameters": schema["parameters"],
                },
                "metadata": metadata,
            }
        )
    normalized.sort(key=lambda item: item["registry_name"])
    return normalized


def build_catalog(server: str, entries: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Build and validate a static catalog without transport configuration."""
    if not isinstance(server, str) or not server.strip():
        raise CatalogValidationError("catalog server must be a non-empty string")
    server = server.strip()
    tools = _validated_tools(entries)
    return {
        "format_version": CATALOG_FORMAT_VERSION,
        "server": server,
        "generation": _generation(server, tools),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tools": tools,
    }


def validate_catalog(payload: Any, *, expected_server: str | None = None) -> dict[str, Any]:
    """Validate an untrusted catalog payload and return canonical data."""
    if not isinstance(payload, dict):
        raise CatalogValidationError("catalog root must be an object")
    unknown = set(payload) - _TOP_LEVEL_FIELDS
    missing = _TOP_LEVEL_FIELDS - set(payload)
    if unknown:
        raise CatalogValidationError(f"catalog has unknown fields: {', '.join(sorted(unknown))}")
    if missing:
        raise CatalogValidationError(f"catalog is missing fields: {', '.join(sorted(missing))}")
    if payload["format_version"] != CATALOG_FORMAT_VERSION:
        raise CatalogValidationError(
            f"unsupported catalog format_version: {payload['format_version']!r}"
        )
    server = payload["server"]
    if not isinstance(server, str) or not server:
        raise CatalogValidationError("catalog server must be a non-empty string")
    if expected_server is not None and server != expected_server:
        raise CatalogValidationError(
            f"catalog server mismatch: expected {expected_server!r}, got {server!r}"
        )
    if not isinstance(payload["created_at"], str) or not payload["created_at"]:
        raise CatalogValidationError("catalog created_at must be a non-empty string")
    tools = _validated_tools(payload["tools"] if isinstance(payload["tools"], list) else [])
    if not isinstance(payload["tools"], list):
        raise CatalogValidationError("catalog tools must be an array")
    expected_generation = _generation(server, tools)
    if payload["generation"] != expected_generation:
        raise CatalogValidationError(
            "catalog generation mismatch; refresh the static MCP catalog"
        )
    return {
        "format_version": CATALOG_FORMAT_VERSION,
        "server": server,
        "generation": expected_generation,
        "created_at": payload["created_at"],
        "tools": tools,
    }


def catalog_root() -> Path:
    return get_hermes_home() / "mcp" / "tool_catalogs"


def catalog_path(server: str, *, root: Path | None = None) -> Path:
    """Return a traversal-safe path derived from the exact logical server name."""
    base = Path(root) if root is not None else catalog_root()
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", server).strip(".-")[:48] or "server"
    suffix = hashlib.sha256(server.encode("utf-8")).hexdigest()[:12]
    return base / f"{slug}-{suffix}.json"


def write_catalog(catalog: dict[str, Any], *, root: Path | None = None) -> Path:
    """Atomically persist a validated catalog with owner-only permissions."""
    validated = validate_catalog(catalog)
    path = catalog_path(validated["server"], root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _canonical_json(validated) + b"\n"
    if len(data) > MAX_CATALOG_BYTES:
        raise CatalogValidationError(
            f"catalog exceeds {MAX_CATALOG_BYTES} byte limit"
        )

    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temp_path.unlink(missing_ok=True)
        raise
    return path


def load_catalog(server: str, *, root: Path | None = None) -> dict[str, Any]:
    """Load and validate a catalog without following a final-path symlink."""
    path = catalog_path(server, root=root)
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        raise CatalogNotFoundError(
            f"no static MCP catalog for {server!r}; refresh it explicitly"
        ) from exc
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise CatalogValidationError("catalog path is not a regular file")
        if metadata.st_size > MAX_CATALOG_BYTES:
            raise CatalogValidationError(
                f"catalog exceeds {MAX_CATALOG_BYTES} byte limit"
            )
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            data = handle.read(MAX_CATALOG_BYTES + 1)
        if len(data) > MAX_CATALOG_BYTES:
            raise CatalogValidationError(
                f"catalog exceeds {MAX_CATALOG_BYTES} byte limit"
            )
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CatalogValidationError(f"invalid catalog JSON: {exc}") from exc
        return validate_catalog(payload, expected_server=server)
    finally:
        if fd >= 0:
            os.close(fd)


__all__ = [
    "CATALOG_FORMAT_VERSION",
    "MAX_CATALOG_BYTES",
    "CatalogNotFoundError",
    "CatalogValidationError",
    "build_catalog",
    "catalog_path",
    "catalog_root",
    "load_catalog",
    "validate_catalog",
    "write_catalog",
]
