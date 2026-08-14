"""Provider fixture and downgrade matrix for Webhook Revolution Task 9."""

import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gateway.platforms.webhook_auth import WebhookAuthMixin


FIXTURE = Path(__file__).parents[1] / "fixtures" / "webhook" / "provider-signature-matrix.json"


def _request(headers):
    request = MagicMock()
    request.headers = headers
    request.match_info = {"route_name": "fixture"}
    return request


def _headers(name, body, secret, now):
    if name in {"github", "github-under-gitlab"}:
        return {"X-Hub-Signature-256": "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()}
    if name == "gitlab":
        return {"X-Gitlab-Token": secret.decode()}
    if name == "svix":
        msg_id = "msg_fixture"
        timestamp = str(now)
        signed = msg_id.encode() + b"." + timestamp.encode() + b"." + body
        signature = base64.b64encode(hmac.new(secret, signed, hashlib.sha256).digest()).decode()
        return {"svix-id": msg_id, "svix-timestamp": timestamp, "svix-signature": "v1," + signature}
    if name == "generic-v2":
        timestamp = str(now)
        signed = timestamp.encode() + b"." + body
        return {
            "X-Webhook-Timestamp": timestamp,
            "X-Webhook-Signature-V2": hmac.new(secret, signed, hashlib.sha256).hexdigest(),
        }
    return {"X-Webhook-Signature": hmac.new(secret, body, hashlib.sha256).hexdigest()}


def test_explicit_provider_fixture_matrix(monkeypatch):
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    body = fixture["body"].encode()
    secret = fixture["secret"].encode()
    now = 1_800_000_000
    monkeypatch.setattr(time, "time", lambda: now)
    adapter = WebhookAuthMixin()

    for case in fixture["cases"]:
        headers = _headers(case["name"], body, secret, now)
        actual = adapter._validate_signature(
            _request(headers), body, secret.decode(), signature_mode=case["mode"]
        )
        assert actual is case["expected"], case["name"]


def test_timestamp_replay_window_is_bidirectional(monkeypatch):
    adapter = WebhookAuthMixin()
    body = b"{}"
    secret = "fixture-secret"
    now = 1_800_000_000
    monkeypatch.setattr(time, "time", lambda: now)
    for timestamp in (now - 301, now + 301):
        raw = str(timestamp)
        signature = hmac.new(secret.encode(), raw.encode() + b"." + body, hashlib.sha256).hexdigest()
        assert not adapter._validate_signature(
            _request({"X-Webhook-Timestamp": raw, "X-Webhook-Signature-V2": signature}),
            body,
            secret,
            signature_mode="generic_v2",
        )


def test_mixed_v1_v2_request_cannot_downgrade_when_timestamp_is_stripped():
    adapter = WebhookAuthMixin()
    body = b"{}"
    secret = "fixture-secret"
    headers = {
        "X-Webhook-Signature-V2": "captured-v2",
        "X-Webhook-Signature": hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(),
    }
    assert not adapter._validate_signature(
        _request(headers), body, secret, signature_mode="generic_v2"
    )
