"""Focused regressions for Freemaxxing local relay hardening."""

import importlib.util
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

_PROXY_PATH = (
    Path(__file__).resolve().parents[3]
    / "plugins"
    / "model-providers"
    / "freemaxxing"
    / "proxy.py"
)
_spec = importlib.util.spec_from_file_location(
    "tests_freemaxxing_proxy_security", _PROXY_PATH
)
_proxy = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _proxy
assert _spec.loader is not None
_spec.loader.exec_module(_proxy)

Backend = _proxy.Backend
pool = _proxy.pool
spawn_proxy = _proxy.spawn_proxy
stop_proxy = _proxy.stop_proxy


def test_authenticated_listener_rejects_missing_token():
    server = spawn_proxy(port=0, token="test-local-token")
    port = server.server_address[1]
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/v1/healthz", timeout=2
        ) as response:
            assert json.loads(response.read()) == {
                "service": "freemaxxing",
                "status": "ok",
            }

        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/healthz", timeout=2
            )
            assert False, "expected detailed health to require auth"
        except urllib.error.HTTPError as exc:
            assert exc.code == 401

        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/healthz",
            headers={"Authorization": "Bearer test-local-token"},
        )
        with urllib.request.urlopen(req, timeout=2) as response:
            payload = json.loads(response.read())
            assert payload["service"] == "freemaxxing"
            assert "health" in payload
    finally:
        stop_proxy(server)
        pool.clear()


def test_router_honors_tier_precedence_and_exclusion():
    pool.clear()
    tier0_a = Backend("tier0-a", "http://127.0.0.1", tier=0)
    tier1 = Backend("tier1", "http://127.0.0.1", tier=1)
    tier0_b = Backend("tier0-b", "http://127.0.0.1", tier=0)
    for backend in (tier0_a, tier1, tier0_b):
        pool.add(backend)

    first = pool.next("freemaxxing")
    assert first is not None
    assert first.tier == 0

    second = pool.next("freemaxxing", exclude={first.name})
    assert second is not None
    assert second.tier == 0
    assert second.name != first.name

    third = pool.next(
        "freemaxxing", exclude={"tier0-a", "tier0-b"}
    )
    assert third is tier1
    pool.clear()


def test_retry_after_is_finite_and_bounded():
    assert _proxy._parse_retry_after({"Retry-After": "nan"}) == 30.0
    assert _proxy._parse_retry_after({"Retry-After": "inf"}) == 30.0
    assert _proxy._parse_retry_after({"Retry-After": "-10"}) == 0.0
    assert _proxy._parse_retry_after({"Retry-After": "9999"}) == 300.0
