"""SEP-2549 schema-cache TTL expiry (tools/mcp_schema_cache.py)."""

import time

import pytest

from tools import mcp_schema_cache as sc


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "_cache_path", lambda: tmp_path / "cache.json")
    yield


def test_entry_without_ttl_never_expires():
    sc.write_cache_entry("srv", "fp", tools=[{"name": "t"}])
    assert sc.get_cached_entry("srv", "fp") is not None


def test_entry_within_ttl_served():
    sc.write_cache_entry("srv", "fp", tools=[{"name": "t"}], ttl_ms=60_000)
    entry = sc.get_cached_entry("srv", "fp")
    assert entry is not None
    assert entry["ttl_ms"] == 60_000
    assert "written_at" in entry


def test_entry_past_ttl_is_a_miss(monkeypatch):
    sc.write_cache_entry("srv", "fp", tools=[{"name": "t"}], ttl_ms=1_000)
    real_time = time.time
    monkeypatch.setattr(sc.time, "time", lambda: real_time() + 2.0)
    assert sc.get_cached_entry("srv", "fp") is None


def test_ttl_rewrite_advances_written_at():
    sc.write_cache_entry("srv", "fp", tools=[{"name": "t"}], ttl_ms=60_000)
    first = sc.get_cached_entry("srv", "fp")["written_at"]
    time.sleep(0.01)
    # Identical payload would previously short-circuit; TTL'd entries must
    # rewrite so written_at advances on every live reconfirmation.
    sc.write_cache_entry("srv", "fp", tools=[{"name": "t"}], ttl_ms=60_000)
    second = sc.get_cached_entry("srv", "fp")["written_at"]
    assert second > first


def test_cache_scope_round_trips():
    sc.write_cache_entry(
        "srv", "fp", tools=[{"name": "t"}], ttl_ms=60_000, cache_scope="private"
    )
    assert sc.get_cached_entry("srv", "fp")["cache_scope"] == "private"


def test_max_ttl_caps_write():
    sc.write_cache_entry("srv", "fp", tools=[{"name": "t"}], ttl_ms=1e12)
    entry = sc.get_cached_entry("srv", "fp")
    assert entry is not None
    assert entry["ttl_ms"] == sc.MAX_TTL_MS


def test_max_ttl_caps_read():
    # Hand-write an entry whose stored ttl_ms exceeds the cap (legacy raw
    # value or hand-edited file): the read treats the effective TTL as
    # MAX_TTL_MS, so an entry older than 24 h is a miss.
    import json

    path = sc._cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    stale_written_at = time.time() - (sc.MAX_TTL_MS / 1000.0) - 1.0
    path.write_text(
        json.dumps(
            {
                "srv::fp": {
                    "epoch": sc.CACHE_SCHEMA_VERSION,
                    "fingerprint": "fp",
                    "ttl_ms": 1e12,
                    "written_at": stale_written_at,
                    "tools": [{"name": "t"}],
                }
            }
        ),
        encoding="utf-8",
    )
    assert sc.get_cached_entry("srv", "fp") is None
    # And a fresh entry under the same raw value IS served (effective TTL
    # clamped to MAX_TTL_MS, still in the future).
    fresh = time.time()
    path.write_text(
        json.dumps(
            {
                "srv::fp": {
                    "epoch": sc.CACHE_SCHEMA_VERSION,
                    "fingerprint": "fp",
                    "ttl_ms": 1e12,
                    "written_at": fresh,
                    "tools": [{"name": "t"}],
                }
            }
        ),
        encoding="utf-8",
    )
    assert sc.get_cached_entry("srv", "fp") is not None


def test_epoch_mismatch_is_a_miss(monkeypatch):
    sc.write_cache_entry("srv", "fp", tools=[{"name": "t"}])
    assert sc.get_cached_entry("srv", "fp") is not None
    monkeypatch.setattr(sc, "CACHE_SCHEMA_VERSION", 99)
    assert sc.get_cached_entry("srv", "fp") is None


def test_config_digest_mismatch_is_a_miss():
    sc.write_cache_entry("srv", "fp", tools=[{"name": "t"}], config_digest="A")
    # config_digest=None (omitted) skips gate 4 — back-compat, served.
    assert sc.get_cached_entry("srv", "fp") is not None
    assert sc.get_cached_entry("srv", "fp", config_digest="B") is None
    assert sc.get_cached_entry("srv", "fp", config_digest="A") is not None
    # A read with a digest never matches an entry written without one
    # (stored None) — fail closed on hand-written/legacy entries.
    sc.write_cache_entry("srv2", "fp", tools=[{"name": "t"}])
    assert sc.get_cached_entry("srv2", "fp", config_digest="X") is None


def test_partition_keying_no_eviction():
    sc.write_cache_entry("srv", "fpA", tools=[{"name": "a"}])
    sc.write_cache_entry("srv", "fpB", tools=[{"name": "b"}])
    assert sc.tools_from_cache_entry(sc.get_cached_entry("srv", "fpA")) == [
        {"name": "a"}
    ]
    assert sc.tools_from_cache_entry(sc.get_cached_entry("srv", "fpB")) == [
        {"name": "b"}
    ]


def test_clear_cache_entry_clears_all_partitions():
    sc.write_cache_entry("srv", "fpA", tools=[{"name": "a"}])
    sc.write_cache_entry("srv", "fpB", tools=[{"name": "b"}])
    sc.write_cache_entry("other", "fpA", tools=[{"name": "o"}])
    sc.clear_cache_entry("srv")
    assert sc.get_cached_entry("srv", "fpA") is None
    assert sc.get_cached_entry("srv", "fpB") is None
    assert sc.get_cached_entry("other", "fpA") is not None


def test_config_digest_hashes_values():
    digest = sc.config_digest({"auth_token": "sekret"})
    assert "sekret" not in digest
    import hashlib
    import json as _json

    raw_hash = hashlib.sha256(
        _json.dumps({"auth_token": "sekret"}, sort_keys=True).encode("utf-8")
    ).hexdigest()
    assert digest != raw_hash[:16]
    # Stable across key order.
    assert sc.config_digest({"a": 1, "b": 2}) == sc.config_digest({"b": 2, "a": 1})
    # Any config edit (even non-fingerprinted keys) changes the digest.
    assert sc.config_digest({"timeout": 5}) != sc.config_digest({"timeout": 6})
