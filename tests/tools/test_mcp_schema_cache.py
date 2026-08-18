"""Unit tests for the on-disk MCP schema cache (tools/mcp_schema_cache.py).

The module landed in #56832's extraction without its tests; these cover the
fingerprint keying, read/write round-trip, and invalidation behavior.
"""

import tools.mcp_schema_cache as msc


class TestConfigFingerprint:
    def test_stable_for_same_config(self):
        cfg = {"command": "npx", "args": ["-y", "@playwright/mcp"]}
        assert msc.config_fingerprint(cfg) == msc.config_fingerprint(dict(cfg))

    def test_changes_when_connection_config_changes(self):
        base = {"command": "npx", "args": ["-y", "@playwright/mcp"]}
        assert msc.config_fingerprint(base) != msc.config_fingerprint(
            {**base, "args": ["-y", "@playwright/mcp", "--headless"]}
        )
        assert msc.config_fingerprint(base) != msc.config_fingerprint(
            {**base, "command": "uvx"}
        )
        assert msc.config_fingerprint(base) != msc.config_fingerprint(
            {**base, "tools": {"include": ["a"]}}
        )

    def test_ignores_non_connection_keys(self):
        base = {"command": "npx", "args": []}
        assert msc.config_fingerprint(base) == msc.config_fingerprint(
            {**base, "timeout": 5, "enabled": True, "lazy": True}
        )

    def test_changes_when_headers_change(self):
        base = {"command": "npx", "args": []}
        assert msc.config_fingerprint(base) != msc.config_fingerprint(
            {**base, "headers": {"X-Tenant": "a"}}
        )
        # Header NAMES are case-insensitive (casing-only edits are stable).
        assert msc.config_fingerprint(
            {**base, "headers": {"X-Tenant": "a"}}
        ) == msc.config_fingerprint(
            {**base, "headers": {"x-tenant": "a"}}
        )
        # Header VALUES are hashed: changing the value changes the partition.
        assert msc.config_fingerprint(
            {**base, "headers": {"X-Tenant": "a"}}
        ) != msc.config_fingerprint(
            {**base, "headers": {"X-Tenant": "b"}}
        )

    def test_changes_when_env_changes(self):
        base = {"command": "npx", "args": []}
        assert msc.config_fingerprint(
            {**base, "env": {"FOO": "a"}}
        ) != msc.config_fingerprint(
            {**base, "env": {"FOO": "b"}}
        )
        # Key order must not change the fingerprint.
        assert msc.config_fingerprint(
            {**base, "env": {"FOO": "a", "BAR": "b"}}
        ) == msc.config_fingerprint(
            {**base, "env": {"BAR": "b", "FOO": "a"}}
        )

    def test_changes_when_auth_context_changes(self):
        base = {"command": "npx", "args": []}
        assert msc.config_fingerprint(
            {**base, "auth": "bearer"}
        ) != msc.config_fingerprint(
            {**base, "auth": "none"}
        )
        assert msc.config_fingerprint(
            {**base, "oauth": {"audience": "x"}}
        ) != msc.config_fingerprint(
            {**base, "oauth": {"audience": "y"}}
        )

    def test_changes_when_identity_header_changes(self, monkeypatch):
        base = {"command": "npx", "args": []}
        alice = {"identity_header": {"name": "X-User-Id", "value_from": "static", "value": "alice"}}
        bob = {"identity_header": {"name": "X-User-Id", "value_from": "static", "value": "bob"}}
        assert msc.config_fingerprint({**base, **alice}) != msc.config_fingerprint(
            {**base, **bob}
        )
        # profile mode resolves to the active profile name — a different
        # principal partitions differently.
        import hermes_cli.profiles as profiles_mod

        monkeypatch.setattr(profiles_mod, "get_active_profile_name", lambda: "work")
        profile_cfg = {"identity_header": {"name": "X-User-Id", "value_from": "profile"}}
        work_fp = msc.config_fingerprint({**base, **profile_cfg})
        monkeypatch.setattr(profiles_mod, "get_active_profile_name", lambda: "personal")
        personal_fp = msc.config_fingerprint({**base, **profile_cfg})
        assert work_fp != personal_fp
        assert work_fp != msc.config_fingerprint({**base, **alice})

    def test_changes_when_protocol_era_changes(self, monkeypatch):
        base = {"command": "npx", "args": []}
        monkeypatch.setattr(msc, "_resolve_protocol_era", lambda: "2025-11-25")
        legacy_fp = msc.config_fingerprint(base)
        monkeypatch.setattr(msc, "_resolve_protocol_era", lambda: "2026-07-28")
        assert msc.config_fingerprint(base) != legacy_fp

    def test_changes_when_epoch_bumps(self, monkeypatch):
        base = {"command": "npx", "args": []}
        v1 = msc.config_fingerprint(base)
        monkeypatch.setattr(msc, "CACHE_SCHEMA_VERSION", 99)
        assert msc.config_fingerprint(base) != v1

    def test_changes_on_ssl_and_cert(self):
        base = {"command": "npx", "args": []}
        assert msc.config_fingerprint(base) != msc.config_fingerprint(
            {**base, "ssl_verify": False}
        )
        assert msc.config_fingerprint(base) != msc.config_fingerprint(
            {**base, "client_cert": "/x.pem"}
        )

    def test_preserves_ignored_keys(self):
        # Non-schema-affecting keys stay out of the fingerprint …
        base = {"command": "npx", "args": []}
        assert msc.config_fingerprint(base) == msc.config_fingerprint(
            {**base, "connect_timeout": 5}
        )
        # … while schema-affecting context keys are covered by it.
        assert msc.config_fingerprint(base) != msc.config_fingerprint(
            {**base, "strict_redirect_headers": True}
        )

    def test_protocol_pin_partitions(self):
        base = {"command": "npx", "args": []}
        assert msc.config_fingerprint(base) != msc.config_fingerprint(
            {**base, "protocol_version": "2025-11-25"}
        )


class TestCacheRoundTrip:
    def _isolate(self, monkeypatch, tmp_path):
        monkeypatch.setattr(msc, "_cache_path", lambda: tmp_path / "cache.json")

    def test_write_then_read_with_matching_fingerprint(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        tools = [{"name": "t1", "description": "d", "inputSchema": {"type": "object"}}]
        msc.write_cache_entry("srv", "fp1", tools=tools, utility_tools=[])
        entry = msc.get_cached_entry("srv", "fp1")
        assert entry is not None
        assert msc.tools_from_cache_entry(entry) == tools
        assert msc.utility_tools_from_cache_entry(entry) == []
        assert msc.has_cached_entry("srv", "fp1")

    def test_fingerprint_mismatch_returns_none(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        msc.write_cache_entry("srv", "fp1", tools=[], utility_tools=[])
        assert msc.get_cached_entry("srv", "OTHER") is None
        assert not msc.has_cached_entry("srv", "OTHER")

    def test_missing_server_returns_none(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        assert msc.get_cached_entry("nope", "fp") is None

    def test_clear_cache_entry(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        msc.write_cache_entry("srv", "fp1", tools=[], utility_tools=[])
        msc.clear_cache_entry("srv")
        assert msc.get_cached_entry("srv", "fp1") is None

    def test_corrupt_cache_file_is_tolerated(self, monkeypatch, tmp_path):
        self._isolate(monkeypatch, tmp_path)
        (tmp_path / "cache.json").write_text("{not json", encoding="utf-8")
        assert msc.get_cached_entry("srv", "fp") is None
        # And writes recover the file.
        msc.write_cache_entry("srv", "fp", tools=[], utility_tools=[])
        assert msc.has_cached_entry("srv", "fp")

    def test_malformed_entry_shapes_are_tolerated(self):
        assert msc.tools_from_cache_entry({"tools": "nope"}) == []
        assert msc.utility_tools_from_cache_entry({}) == []


class TestCacheFileLocation:
    def test_cache_lives_under_hermes_home_cache_dir_with_0600(
        self, monkeypatch, tmp_path
    ):
        # Real path (no _cache_path monkeypatch): HERMES_HOME/cache/…, 0o600,
        # matching the discovery-cache precedent in tools/registry.py.
        import hermes_constants

        monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
        path = msc._cache_path()
        assert path == tmp_path / "cache" / "mcp_schema_cache.json"
        msc.write_cache_entry("srv", "fp", tools=[], utility_tools=[])
        assert path.exists()
        assert (path.stat().st_mode & 0o777) == 0o600


class TestWriteSkip:
    def test_identical_payload_skips_rewrite(self, monkeypatch, tmp_path):
        monkeypatch.setattr(msc, "_cache_path", lambda: tmp_path / "cache.json")
        saves = []
        real_save = msc._save_all

        def _counting_save(data):
            saves.append(1)
            real_save(data)

        monkeypatch.setattr(msc, "_save_all", _counting_save)
        tools = [{"name": "t1", "description": "d", "inputSchema": {}}]
        msc.write_cache_entry("srv", "fp1", tools=tools, utility_tools=[])
        assert len(saves) == 1
        # Identical payload (reconnect / list_changed refresh) → no rewrite.
        msc.write_cache_entry("srv", "fp1", tools=list(tools), utility_tools=[])
        assert len(saves) == 1
        # Changed payload → rewrite.
        msc.write_cache_entry("srv", "fp2", tools=tools, utility_tools=[])
        assert len(saves) == 2
