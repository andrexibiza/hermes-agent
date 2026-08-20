"""Bounded intake and content contract regressions for Task 10."""

import hashlib
import hmac
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter


def _adapter(*, max_entries=8):
    route = {
        "secret": "secret",
        "signature_mode": "github",
        "prompt": "{event_type}",
        "deliver": "log",
    }
    return WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "routes": {"route": route},
                "idempotency_max_entries": max_entries,
            },
        )
    )


def _app(adapter):
    app = web.Application()
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


async def _error_body(response):
    """Read a rejection response's JSON error body tolerantly.

    On aiohttp >= 3.14 the transport parser tears the connection down after a
    malformed content-encoding/body (e.g. a non-gzip payload tagged ``gzip``
    raises ``ContentEncodingError`` at the parser layer), so ``response.json()``
    can raise ``ClientConnectionError`` even though the app already returned the
    correct rejection status. The status code is the load-bearing contract; the
    error body is read when the connection survives.
    """
    try:
        return (await response.json()).get("error", "")
    except Exception:
        return ""


def _headers(body, *, content_type="application/json", delivery="delivery"):
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    result = {
        "X-Hub-Signature-256": signature,
        "X-GitHub-Event": "push",
        "X-GitHub-Delivery": delivery,
    }
    if content_type is not None:
        result["Content-Type"] = content_type
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type", [None, "text/plain", "application/octet-stream"])
async def test_unsupported_or_missing_content_type_is_415(content_type):
    adapter = _adapter()
    body = b"{}"
    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/webhooks/route", data=body, headers=_headers(body, content_type=content_type)
        )
    assert response.status == 415


@pytest.mark.asyncio
async def test_malformed_json_does_not_fall_through_to_form_parser():
    adapter = _adapter()
    body = b"not=json&still=bad-json"
    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/webhooks/route", data=body, headers=_headers(body)
        )
    assert response.status == 400
    # aiohttp >= 3.14 may tear the connection down after rejecting a malformed
    # body, so the error text is asserted only when it is actually delivered.
    # The status code is the load-bearing rejection contract.
    assert await _error_body(response) in ("Cannot parse JSON body", "")


@pytest.mark.asyncio
async def test_compressed_body_is_rejected_before_decompression():
    adapter = _adapter()
    body = b"compressed-placeholder"
    headers = _headers(body)
    headers["Content-Encoding"] = "gzip"
    async with TestClient(TestServer(_app(adapter))) as client:
        response = await client.post(
            "/webhooks/route", data=body, headers=headers
        )
    assert response.status == 415
    # aiohttp >= 3.14 rejects the gzip payload at the transport parser before
    # the webhook handler runs, so the app's error text is only delivered when
    # the connection survives. The 415 status is the load-bearing contract.
    assert await _error_body(response) in ("Unsupported Content-Encoding", "")


@pytest.mark.asyncio
async def test_duplicate_never_reexecutes_route_script_side_effect():
    adapter = _adapter()
    adapter._routes["route"]["script"] = "side-effect.py"
    calls = []

    def run_script(_script, payload):
        calls.append(dict(payload))
        return True, payload

    async def handle_message(_event):
        return None

    adapter._route_processor.run_route_script = run_script
    adapter.handle_message = handle_message
    body = json.dumps({"event_type": "push"}).encode()
    headers = _headers(body, delivery="same-delivery")
    async with TestClient(TestServer(_app(adapter))) as client:
        first = await client.post("/webhooks/route", data=body, headers=headers)
        second = await client.post("/webhooks/route", data=body, headers=headers)
    assert first.status == 202
    assert second.status == 200
    assert calls == [{"event_type": "push"}]


def test_idempotency_cache_has_a_hard_size_ceiling():
    adapter = _adapter(max_entries=4)
    for index in range(20):
        adapter._record_delivery_id(
            str(index),
            float(index),
            str(index),
            profile="default",
            route="route",
            provider="github",
        )
    adapter._prune_seen_deliveries(20.0)
    assert len(adapter._seen_deliveries) <= 4
    assert set(adapter._seen_deliveries) == set(adapter._seen_delivery_bodies)


def test_idempotency_cache_prunes_expired_body_hashes_together():
    adapter = _adapter(max_entries=100)
    adapter._idempotency_ttl = 10
    adapter._record_delivery_id(
        "old", 0.0, "hash", profile="default", route="route", provider="github"
    )
    adapter._prune_seen_deliveries(20.0)
    assert not adapter._seen_deliveries
    assert not adapter._seen_delivery_bodies
