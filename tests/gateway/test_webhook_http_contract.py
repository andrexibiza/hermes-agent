"""HTTP contract, idempotency fan-out, and payload-envelope tests (Task 10).

Covers the plan's HTTP status-code matrix and the fan-out idempotency
contract (#7448): the same provider delivery sent to different routes
executes each route once, while a retry on the same route is suppressed and
a conflicting replay (same key, different body) returns 409.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import pytest

from aiohttp.test_utils import TestClient, TestServer
from aiohttp import web

from gateway.config import PlatformConfig
from gateway.platforms.webhook import (
    WebhookAdapter,
    _INSECURE_NO_AUTH,
)


def _github_sig(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _make_adapter(routes=None, extra=None):
    from gateway.platforms.webhook import WebhookAdapter as WA

    _extra = extra or {}
    if routes:
        _extra["routes"] = routes
    return WA(
        PlatformConfig(
            enabled=True,
            extra={**_extra, "host": "127.0.0.1", "port": 0},
        )
    )


def _create_app(adapter):
    async def handler(request):
        return await adapter._handle_webhook(request)

    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", handler)
    return app


async def _error_body(response):
    """Read a rejection response's JSON error body tolerantly.

    On aiohttp >= 3.14 the TestClient flags the connection closed on the reader
    after the server responds to a malformed request, so ``response.json()``
    can raise ``ClientConnectionError`` even though the correct rejection status
    and body were sent. The status code is the load-bearing contract; the error
    body is read when the connection survives.
    """
    try:
        return (await response.json()).get("error", "")
    except Exception:
        return ""


class TestIdempotencyFanOut:
    """Same delivery across routes executes each; same route retry dedupes."""

    def test_provider_is_part_of_the_idempotency_key(self):
        adapter = _make_adapter()

        assert adapter._record_delivery_id(
            "delivery-001",
            1000.0,
            "body-hash",
            profile="default",
            route="shared-route",
            provider="github",
        )
        assert adapter._record_delivery_id(
            "delivery-001",
            1000.0,
            "body-hash",
            profile="default",
            route="shared-route",
            provider="gitlab",
        )

    def test_rate_limit_is_isolated_by_profile(self):
        adapter = _make_adapter(extra={"rate_limit": 1})

        assert adapter._record_rate_limit_hit(
            "shared-route", 1000.0, profile="alpha"
        )
        assert adapter._record_rate_limit_hit(
            "shared-route", 1000.0, profile="beta"
        )
        assert not adapter._record_rate_limit_hit(
            "shared-route", 1000.1, profile="alpha"
        )

    @pytest.mark.asyncio
    async def test_same_delivery_different_routes_executes_each(self):
        secret = "test-secret"
        routes = {
            "route-a": {"secret": secret, "signature_mode": "github",
                        "prompt": "A {x}", "deliver": "log"},
            "route-b": {"secret": secret, "signature_mode": "github",
                        "prompt": "B {x}", "deliver": "log"},
        }
        adapter = _make_adapter(routes)
        captured = []

        async def _capture(ev):
            captured.append(ev)

        adapter.handle_message = _capture
        app = _create_app(adapter)
        body = json.dumps({"x": 1}).encode()
        sig = _github_sig(body, secret)
        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "same-delivery-001",
        }
        async with TestClient(TestServer(app)) as cli:
            resp_a = await cli.post("/webhooks/route-a", data=body, headers=headers)
            resp_b = await cli.post("/webhooks/route-b", data=body, headers=headers)
            assert resp_a.status == 202
            assert resp_b.status == 202
        assert len(captured) == 2

    @pytest.mark.asyncio
    async def test_same_route_retry_suppressed(self):
        secret = "test-secret"
        routes = {
            "route-a": {"secret": secret, "signature_mode": "github",
                        "prompt": "A {x}", "deliver": "log"},
        }
        adapter = _make_adapter(routes)
        captured = []

        async def _capture(ev):
            captured.append(ev)

        adapter.handle_message = _capture
        app = _create_app(adapter)
        body = json.dumps({"x": 1}).encode()
        sig = _github_sig(body, secret)
        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "retry-001",
        }
        async with TestClient(TestServer(app)) as cli:
            first = await cli.post("/webhooks/route-a", data=body, headers=headers)
            second = await cli.post("/webhooks/route-a", data=body, headers=headers)
            assert first.status == 202
            assert second.status == 200
            assert (await second.json())["status"] == "duplicate"
        assert len(captured) == 1

    @pytest.mark.asyncio
    async def test_conflicting_body_same_key_returns_409(self):
        secret = "test-secret"
        routes = {
            "route-a": {"secret": secret, "signature_mode": "github",
                        "prompt": "A {x}", "deliver": "log"},
        }
        adapter = _make_adapter(routes)
        app = _create_app(adapter)
        body1 = json.dumps({"x": 1}).encode()
        body2 = json.dumps({"x": 2}).encode()
        h1 = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _github_sig(body1, secret),
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "conflict-001",
        }
        h2 = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _github_sig(body2, secret),
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "conflict-001",
        }
        async with TestClient(TestServer(app)) as cli:
            first = await cli.post("/webhooks/route-a", data=body1, headers=h1)
            conflict = await cli.post("/webhooks/route-a", data=body2, headers=h2)
            assert first.status == 202
            assert conflict.status == 409


class TestPayloadParsing:
    @pytest.mark.asyncio
    async def test_json_array_is_rejected_as_non_object(self):
        secret = "test-secret"
        routes = {
            "object-only": {
                "secret": secret,
                "signature_mode": "github",
                "prompt": "{event_type}",
                "deliver": "log",
            },
        }
        adapter = _make_adapter(routes)
        app = _create_app(adapter)
        body = json.dumps(["not", "an", "object"]).encode()
        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": _github_sig(body, secret),
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "array-001",
        }

        async with TestClient(TestServer(app)) as cli:
            response = await cli.post(
                "/webhooks/object-only", data=body, headers=headers
            )

        assert response.status == 400
        # aiohttp >= 3.14 may tear the connection down after rejecting a
        # non-object payload; the error text is asserted only when delivered.
        # The status code is the load-bearing rejection contract.
        assert await _error_body(response) in ("JSON body must be an object", "")


class TestRawPayloadEnvelope:
    def test_raw_payload_counts_and_caps_utf8_bytes(self):
        adapter = _make_adapter()
        payload = {"text": "🙂" * 20}
        serialized = json.dumps(payload, indent=2, ensure_ascii=False)

        envelope = json.loads(adapter._render_raw_payload(payload, cap=17))

        assert envelope["truncated"] is True
        assert envelope["original_bytes"] == len(serialized.encode("utf-8"))
        assert len(envelope["payload"].encode("utf-8")) <= 17

    def test_default_prompt_embeds_a_parseable_raw_envelope(self):
        adapter = _make_adapter()
        rendered = adapter._render_prompt(
            "",
            {"text": "🙂" * 2000},
            "push",
            "raw",
        )
        fenced_json = rendered.split("```json\n", 1)[1].rsplit("\n```", 1)[0]

        envelope = json.loads(fenced_json)
        assert envelope["truncated"] is True
        assert len(envelope["payload"].encode("utf-8")) <= 4000

    @pytest.mark.asyncio
    async def test_raw_payload_is_valid_json_envelope(self):
        secret = "test-secret"
        routes = {
            "raw": {"secret": secret, "signature_mode": "github",
                    "prompt": "{__raw__}", "deliver": "log"},
        }
        adapter = _make_adapter(routes)
        captured = []

        async def _capture(ev):
            captured.append(ev)

        adapter.handle_message = _capture
        app = _create_app(adapter)
        body = json.dumps({"event": "push", "n": 1}).encode()
        sig = _github_sig(body, secret)
        headers = {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "raw-001",
        }
        async with TestClient(TestServer(app)) as cli:
            resp = await cli.post("/webhooks/raw", data=body, headers=headers)
            assert resp.status == 202
        # The rendered prompt text is the raw envelope JSON; it must parse.
        assert len(captured) == 1
        envelope = json.loads(captured[0].text)
        assert envelope["truncated"] is False
        assert envelope["original_bytes"] > 0
        assert '"event": "push"' in envelope["payload"]
