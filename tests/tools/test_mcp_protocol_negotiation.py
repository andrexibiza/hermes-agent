"""MCP 2026-07-28 protocol-era negotiation (_negotiate_session).

The negotiation helper decides between the legacy ``initialize`` handshake
and the stateless ``server/discover`` probe (SEP-2575) per the per-server
``protocol`` config key. These tests drive it with duck-typed sessions —
the live-path integration is covered by the real-server dual-SDK E2E in
``tests/tools/test_mcp_dual_era_wire.py`` (runs under the
mcp-dual-era-conformance CI job; skips where MCP_LEGACY_PYTHON is unset).
"""

import asyncio

import pytest

from tools.mcp_tool import (
    MCPServerTask,
    _handshake_rejected_as_modern,
    _JSONRPC_UNSUPPORTED_PROTOCOL_VERSION,
    _resolve_protocol_policy,
)


class _Err(Exception):
    def __init__(self, code, msg="err"):
        super().__init__(msg)
        self.error = type("E", (), {"code": code})()


class _Session:
    def __init__(self, init=None, disc=None):
        self._init = init
        self._disc = disc
        self.calls = []

    async def initialize(self):
        self.calls.append("initialize")
        if isinstance(self._init, Exception):
            raise self._init
        return self._init

    async def discover(self):
        self.calls.append("discover")
        if isinstance(self._disc, Exception):
            raise self._disc
        return self._disc


class _LegacySession:
    """mcp 1.x sessions have no discover() attribute at all."""

    def __init__(self, init=None):
        self._init = init
        self.calls = []

    async def initialize(self):
        self.calls.append("initialize")
        if isinstance(self._init, Exception):
            raise self._init
        return self._init


def _task(protocol=None):
    t = MCPServerTask("negotest")
    t._config = {} if protocol is None else {"protocol": protocol}
    return t


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestAutoMode:
    def test_handshake_first_no_discover_on_success(self):
        s = _Session(init="INIT_RESULT")
        out = _run(_task()._negotiate_session(s, 5))
        assert out == "INIT_RESULT"
        assert s.calls == ["initialize"]

    def test_falls_back_to_discover_on_unsupported_protocol_version(self):
        s = _Session(init=_Err(_JSONRPC_UNSUPPORTED_PROTOCOL_VERSION), disc="DISC_RESULT")
        out = _run(_task()._negotiate_session(s, 5))
        assert out == "DISC_RESULT"
        assert s.calls == ["initialize", "discover"]

    def test_falls_back_on_method_not_found(self):
        s = _Session(init=_Err(-32601, "Method not found: initialize"), disc="DISC_RESULT")
        out = _run(_task()._negotiate_session(s, 5))
        assert out == "DISC_RESULT"

    def test_unrelated_error_propagates_without_discover(self):
        s = _Session(init=_Err(-32000, "borked"))
        with pytest.raises(_Err):
            _run(_task()._negotiate_session(s, 5))
        assert s.calls == ["initialize"]

    def test_timeout_propagates_not_swallowed(self):
        class _Hang(_Session):
            async def initialize(self):
                await asyncio.sleep(30)

        with pytest.raises(asyncio.TimeoutError):
            _run(_task()._negotiate_session(_Hang(), 0.05))


class TestExplicitModes:
    def test_stateless_probes_discover_first(self):
        s = _Session(init="INIT_RESULT", disc="DISC_RESULT")
        out = _run(_task("stateless")._negotiate_session(s, 5))
        assert out == "DISC_RESULT"
        assert s.calls == ["discover"]

    def test_stateless_falls_back_to_handshake(self):
        s = _Session(init="INIT_RESULT", disc=_Err(-32601))
        out = _run(_task("stateless")._negotiate_session(s, 5))
        assert out == "INIT_RESULT"
        assert s.calls == ["discover", "initialize"]

    def test_legacy_never_discovers(self):
        s = _Session(init=_Err(_JSONRPC_UNSUPPORTED_PROTOCOL_VERSION), disc="DISC_RESULT")
        with pytest.raises(_Err):
            _run(_task("legacy")._negotiate_session(s, 5))
        assert s.calls == ["initialize"]

    def test_unknown_mode_rejected(self):
        s = _Session(init="INIT_RESULT")
        with pytest.raises(ValueError, match="not a valid policy"):
            _run(_task("bogus")._negotiate_session(s, 5))
        assert s.calls == []

    def test_non_string_protocol_rejected(self):
        for bad in (None, 42):
            t = _task()
            t._config = {"protocol": bad}
            s = _Session(init="INIT_RESULT")
            with pytest.raises(ValueError, match="protocol must be a string"):
                _run(t._negotiate_session(s, 5))
            assert s.calls == []

    def test_legacy_sdk_session_without_discover_reraises(self):
        # mcp 1.x ClientSession has no .discover(): the auto fallback must
        # re-raise the original handshake error, not AttributeError.
        s = _LegacySession(init=_Err(_JSONRPC_UNSUPPORTED_PROTOCOL_VERSION))
        with pytest.raises(_Err):
            _run(_task()._negotiate_session(s, 5))
        assert s.calls == ["initialize"]


class TestModernRejectionClassifier:
    def test_structural_codes(self):
        assert _handshake_rejected_as_modern(_Err(-32022))
        assert _handshake_rejected_as_modern(_Err(-32601))
        assert not _handshake_rejected_as_modern(_Err(-32000))

    def test_substring_fallbacks(self):
        assert _handshake_rejected_as_modern(Exception("Unsupported protocol version"))
        assert _handshake_rejected_as_modern(Exception("Unknown method: initialize"))
        assert not _handshake_rejected_as_modern(Exception("connection reset by peer"))


class TestPolicyResolution:
    def test_aliases_map(self):
        assert _resolve_protocol_policy("auto") == "prefer-legacy"
        assert _resolve_protocol_policy("handshake") == "legacy"
        assert _resolve_protocol_policy("stateless") == "prefer-modern"
        assert _resolve_protocol_policy("modern") == "prefer-modern"
        assert _resolve_protocol_policy("2026-07-28") == "prefer-modern"
        assert _resolve_protocol_policy("legacy") == "legacy"
        assert _resolve_protocol_policy("prefer-legacy") == "prefer-legacy"
        assert _resolve_protocol_policy("prefer-modern") == "prefer-modern"
        assert _resolve_protocol_policy("strict-modern") == "strict-modern"
        # Case/whitespace tolerance, like the old mode normalization.
        assert _resolve_protocol_policy("  AUTO ") == "prefer-legacy"

    def test_unknown_value_rejected(self):
        for bad in ("bogus", "dual", "hybrid", ""):
            with pytest.raises(ValueError, match="not a valid policy"):
                _resolve_protocol_policy(bad)

    def test_non_string_rejected(self):
        for bad in (None, 42, ["auto"], {"protocol": "legacy"}):
            with pytest.raises(ValueError, match="protocol must be a string"):
                _resolve_protocol_policy(bad)


class TestPreferModernMode:
    def test_discover_first_success(self):
        s = _Session(init="INIT_RESULT", disc="DISC_RESULT")
        out = _run(_task("prefer-modern")._negotiate_session(s, 5))
        assert out == "DISC_RESULT"
        assert s.calls == ["discover"]

    def test_falls_back_on_method_not_found(self):
        s = _Session(init="INIT_RESULT", disc=_Err(-32601))
        out = _run(_task("prefer-modern")._negotiate_session(s, 5))
        assert out == "INIT_RESULT"
        assert s.calls == ["discover", "initialize"]

    def test_falls_back_on_unsupported_protocol_version(self):
        s = _Session(init="INIT_RESULT", disc=_Err(_JSONRPC_UNSUPPORTED_PROTOCOL_VERSION))
        out = _run(_task("prefer-modern")._negotiate_session(s, 5))
        assert out == "INIT_RESULT"
        assert s.calls == ["discover", "initialize"]

    def test_invalid_params_propagates_without_fallback(self):
        # NEW BEHAVIOR (the #88698 defect fix): a non-exact signal must NOT
        # silently downgrade the connection to the handshake era.
        s = _Session(init="INIT_RESULT", disc=_Err(-32602))
        with pytest.raises(_Err):
            _run(_task("prefer-modern")._negotiate_session(s, 5))
        assert s.calls == ["discover"]

    def test_internal_error_propagates_without_fallback(self):
        s = _Session(init="INIT_RESULT", disc=_Err(-32000, "borked"))
        with pytest.raises(_Err):
            _run(_task("prefer-modern")._negotiate_session(s, 5))
        assert s.calls == ["discover"]

    def test_timeout_propagates_without_fallback(self):
        class _Hang(_Session):
            async def discover(self):
                await asyncio.sleep(30)

        with pytest.raises(asyncio.TimeoutError):
            _run(_task("prefer-modern")._negotiate_session(_Hang(), 0.05))


class TestStrictModernMode:
    def test_discover_first_success(self):
        s = _Session(init="INIT_RESULT", disc="DISC_RESULT")
        out = _run(_task("strict-modern")._negotiate_session(s, 5))
        assert out == "DISC_RESULT"
        assert s.calls == ["discover"]

    def test_method_not_found_propagates_no_downgrade(self):
        # strict-modern refuses to downgrade even on the exact modern
        # rejection signals.
        s = _Session(init="INIT_RESULT", disc=_Err(-32601))
        with pytest.raises(_Err):
            _run(_task("strict-modern")._negotiate_session(s, 5))
        assert s.calls == ["discover"]

    def test_unsupported_protocol_version_propagates(self):
        s = _Session(init="INIT_RESULT", disc=_Err(_JSONRPC_UNSUPPORTED_PROTOCOL_VERSION))
        with pytest.raises(_Err):
            _run(_task("strict-modern")._negotiate_session(s, 5))
        assert s.calls == ["discover"]


class TestPreferLegacyMode:
    def test_initialize_first_success(self):
        s = _Session(init="INIT_RESULT", disc="DISC_RESULT")
        out = _run(_task("prefer-legacy")._negotiate_session(s, 5))
        assert out == "INIT_RESULT"
        assert s.calls == ["initialize"]

    def test_falls_back_to_discover_on_unsupported_protocol_version(self):
        s = _Session(init=_Err(_JSONRPC_UNSUPPORTED_PROTOCOL_VERSION), disc="DISC_RESULT")
        out = _run(_task("prefer-legacy")._negotiate_session(s, 5))
        assert out == "DISC_RESULT"
        assert s.calls == ["initialize", "discover"]

    def test_falls_back_to_discover_on_method_not_found(self):
        s = _Session(init=_Err(-32601, "Method not found: initialize"), disc="DISC_RESULT")
        out = _run(_task("prefer-legacy")._negotiate_session(s, 5))
        assert out == "DISC_RESULT"

    def test_unrelated_error_propagates(self):
        s = _Session(init=_Err(-32000, "borked"), disc="DISC_RESULT")
        with pytest.raises(_Err):
            _run(_task("prefer-legacy")._negotiate_session(s, 5))
        assert s.calls == ["initialize"]

    def test_legacy_sdk_session_without_discover_reraises(self):
        s = _LegacySession(init=_Err(_JSONRPC_UNSUPPORTED_PROTOCOL_VERSION))
        with pytest.raises(_Err):
            _run(_task("prefer-legacy")._negotiate_session(s, 5))
        assert s.calls == ["initialize"]


class TestNegotiationObservability:
    def test_success_logs_policy_and_dialect(self, caplog):
        import logging

        s = _Session(init="INIT_RESULT")
        with caplog.at_level(logging.INFO, logger="tools.mcp_tool"):
            out = _run(_task("prefer-legacy")._negotiate_session(s, 5))
        assert out == "INIT_RESULT"
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "protocol policy=prefer-legacy" in joined
        assert "connected via legacy initialize handshake" in joined

    def test_fallback_logs_trigger_signal(self, caplog):
        import logging

        s = _Session(init=_Err(_JSONRPC_UNSUPPORTED_PROTOCOL_VERSION), disc="DISC_RESULT")
        with caplog.at_level(logging.INFO, logger="tools.mcp_tool"):
            out = _run(_task("prefer-legacy")._negotiate_session(s, 5))
        assert out == "DISC_RESULT"
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "after fallback signal" in joined
        assert str(_JSONRPC_UNSUPPORTED_PROTOCOL_VERSION) in joined
