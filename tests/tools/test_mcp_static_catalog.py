import json
import stat

import pytest


def _entries():
    return [
        {
            "registry_name": "mcp__docs__lookup",
            "remote_name": "lookup",
            "kind": "tool",
            "schema": {
                "name": "mcp__docs__lookup",
                "description": "Look up documentation",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        }
    ]


def test_catalog_round_trip_is_generation_bound_and_secret_free(tmp_path):
    from tools.mcp_static_catalog import build_catalog, load_catalog, write_catalog

    catalog = build_catalog("../docs server", _entries())
    path = write_catalog(catalog, root=tmp_path)

    assert path.parent == tmp_path
    assert path.name.endswith(".json")
    assert ".." not in path.name
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    raw = path.read_text(encoding="utf-8")
    assert "command" not in raw
    assert "headers" not in raw
    assert "env" not in raw
    assert "token" not in raw.lower()

    loaded = load_catalog("../docs server", root=tmp_path)
    assert loaded == catalog
    assert loaded["generation"].startswith("sha256:")


def test_tampered_catalog_generation_is_rejected(tmp_path):
    from tools.mcp_static_catalog import (
        CatalogValidationError,
        build_catalog,
        catalog_path,
        load_catalog,
        write_catalog,
    )

    catalog = build_catalog("docs", _entries())
    write_catalog(catalog, root=tmp_path)
    path = catalog_path("docs", root=tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["tools"][0]["schema"]["description"] = "tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CatalogValidationError, match="generation"):
        load_catalog("docs", root=tmp_path)


def test_catalog_rejects_duplicate_names_and_unknown_fields():
    from tools.mcp_static_catalog import CatalogValidationError, build_catalog

    duplicate = _entries() + _entries()
    with pytest.raises(CatalogValidationError, match="duplicate"):
        build_catalog("docs", duplicate)

    with_extra = _entries()
    with_extra[0]["command"] = "steal-secret"
    with pytest.raises(CatalogValidationError, match="unknown fields"):
        build_catalog("docs", with_extra)


def test_loading_wrong_server_catalog_is_rejected(tmp_path):
    from tools.mcp_static_catalog import (
        CatalogValidationError,
        build_catalog,
        catalog_path,
        load_catalog,
        write_catalog,
    )

    catalog = build_catalog("docs", _entries())
    write_catalog(catalog, root=tmp_path)
    source = catalog_path("docs", root=tmp_path)
    target = catalog_path("other", root=tmp_path)
    target.write_bytes(source.read_bytes())

    with pytest.raises(CatalogValidationError, match="server"):
        load_catalog("other", root=tmp_path)
