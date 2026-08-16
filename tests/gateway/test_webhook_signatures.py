"""Tests for explicit, mode-bound webhook signature verification.

These prove the Task 9 contract: a route's ``signature_mode`` decides the
validation scheme, never header-driven inference. A route configured for
one provider must reject another provider's headers even if they would have
validated under the old inference logic.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

import pytest

from gateway.platforms.webhook_auth import WebhookAuthMixin


def _make_adapter():
    """Return a minimal WebhookAuthMixin instance for direct method tests."""
    return WebhookAuthMixin()


def _mock_request(headers: dict, route_name: str = "test-route"):
    from unittest.mock import MagicMock

    req = MagicMock()
    req.headers = headers
    req.match_info = {"route_name": route_name}
    req.method = "POST"
    return req


def _github_signature(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()


def _gitlab_token(secret: str) -> str:
    return secret


def _generic_v2_signature(body: bytes, secret: str, timestamp: str) -> str:
    signed = timestamp.encode() + b"." + body
    return hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()


def _generic_v1_signature(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _svix_signature(body: bytes, secret: str, msg_id: str, timestamp: str) -> str:
    signed = msg_id.encode() + b"." + timestamp.encode() + b"." + body
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


BODY = b'{"event": "push"}'
SECRET = "test-secret-key"


class TestModeBoundVerification:
    def test_github_mode_accepts_github_rejects_gitlab(self):
        adapter = _make_adapter()
        req = _mock_request(headers={"X-Hub-Signature-256": _github_signature(BODY, SECRET)})
        assert adapter._validate_signature(req, BODY, SECRET, signature_mode="github") is True

        # GitLab token present but mode is github → reject
        req2 = _mock_request(headers={"X-Gitlab-Token": SECRET})
        assert adapter._validate_signature(req2, BODY, SECRET, signature_mode="github") is False

    def test_gitlab_mode_accepts_gitlab_rejects_github(self):
        adapter = _make_adapter()
        req = _mock_request(headers={"X-Gitlab-Token": SECRET})
        assert adapter._validate_signature(req, BODY, SECRET, signature_mode="gitlab") is True

        # GitHub signature present but mode is gitlab → reject
        req2 = _mock_request(headers={"X-Hub-Signature-256": _github_signature(BODY, SECRET)})
        assert adapter._validate_signature(req2, BODY, SECRET, signature_mode="gitlab") is False

    def test_generic_v2_accepts_v2_only(self):
        adapter = _make_adapter()
        ts = str(int(time.time()))
        req = _mock_request(headers={
            "X-Webhook-Signature-V2": _generic_v2_signature(BODY, SECRET, ts),
            "X-Webhook-Timestamp": ts,
        })
        assert adapter._validate_signature(req, BODY, SECRET, signature_mode="generic_v2") is True

    def test_generic_v2_rejects_v1_when_configured_v2(self):
        # A V1 signature alone must NOT validate under generic_v2 mode.
        adapter = _make_adapter()
        req = _mock_request(headers={"X-Webhook-Signature": _generic_v1_signature(BODY, SECRET)})
        assert adapter._validate_signature(req, BODY, SECRET, signature_mode="generic_v2") is False

    def test_generic_v1_accepts_v1_only_when_configured_v1(self):
        adapter = _make_adapter()
        req = _mock_request(headers={"X-Webhook-Signature": _generic_v1_signature(BODY, SECRET)})
        assert adapter._validate_signature(req, BODY, SECRET, signature_mode="generic_v1") is True


class TestReplaySafety:
    def test_generic_v2_stripped_timestamp_rejects_no_downgrade(self):
        # Mixed V1+V2 captured, timestamp stripped, V1 still present.
        # generic_v2 mode must reject (never downgrade to V1).
        adapter = _make_adapter()
        v2_sig = _generic_v2_signature(BODY, SECRET, str(int(time.time())))
        v1_sig = _generic_v1_signature(BODY, SECRET)
        req = _mock_request(headers={
            "X-Webhook-Signature-V2": v2_sig,
            "X-Webhook-Signature": v1_sig,
            # X-Webhook-Timestamp deliberately omitted.
        })
        assert adapter._validate_signature(req, BODY, SECRET, signature_mode="generic_v2") is False

    def test_generic_v2_expired_timestamp_rejects(self):
        adapter = _make_adapter()
        old_ts = str(int(time.time()) - 10000)
        req = _mock_request(headers={
            "X-Webhook-Signature-V2": _generic_v2_signature(BODY, SECRET, old_ts),
            "X-Webhook-Timestamp": old_ts,
        })
        assert adapter._validate_signature(req, BODY, SECRET, signature_mode="generic_v2") is False

    def test_generic_v2_future_timestamp_rejects(self):
        adapter = _make_adapter()
        future_ts = str(int(time.time()) + 10000)
        req = _mock_request(headers={
            "X-Webhook-Signature-V2": _generic_v2_signature(BODY, SECRET, future_ts),
            "X-Webhook-Timestamp": future_ts,
        })
        assert adapter._validate_signature(req, BODY, SECRET, signature_mode="generic_v2") is False

    def test_svix_stale_timestamp_rejects(self):
        adapter = _make_adapter()
        msg_id = "msg_123"
        old_ts = str(int(time.time()) - 10000)
        sig = _svix_signature(BODY, SECRET, msg_id, old_ts)
        req = _mock_request(headers={
            "svix-id": msg_id,
            "svix-timestamp": old_ts,
            "svix-signature": sig,
        })
        assert adapter._validate_signature(req, BODY, SECRET, signature_mode="svix") is False


class TestMalformedAndAttack:
    def test_non_ascii_signature_fails_closed(self):
        adapter = _make_adapter()
        req = _mock_request(headers={"X-Hub-Signature-256": "sha256=\u00e9\u00e8\u00ea"})
        assert adapter._validate_signature(req, BODY, SECRET, signature_mode="github") is False

    def test_changed_body_rejects(self):
        adapter = _make_adapter()
        req = _mock_request(headers={"X-Hub-Signature-256": _github_signature(BODY, SECRET)})
        tampered = b'{"event": "delete"}'
        assert adapter._validate_signature(req, tampered, SECRET, signature_mode="github") is False

    def test_rotated_signature_rejects_old(self):
        adapter = _make_adapter()
        old_sig = _github_signature(BODY, "old-secret")
        req = _mock_request(headers={"X-Hub-Signature-256": old_sig})
        assert adapter._validate_signature(req, BODY, SECRET, signature_mode="github") is False

    def test_missing_required_header_rejects(self):
        adapter = _make_adapter()
        req = _mock_request(headers={})  # no signature at all
        assert adapter._validate_signature(req, BODY, SECRET, signature_mode="github") is False

    def test_unknown_mode_fails_closed(self):
        adapter = _make_adapter()
        req = _mock_request(headers={"X-Hub-Signature-256": _github_signature(BODY, SECRET)})
        assert adapter._validate_signature(req, BODY, SECRET, signature_mode="bogus") is False


class TestGitlabStandardMode:
    """GitLab Standard Webhooks (issue #47451, impl by HwangJohn in #47849).

    The wire contract: webhook-id / webhook-timestamp / webhook-signature
    headers, signed content "{id}.{timestamp}.{raw_body}", signature is
    "v1,<base64-hmac-sha256>". Must validate only in gitlab_standard mode,
    never under another mode via header presence.
    """

    def _standard_signature(self, body, secret, msg_id, timestamp):
        signed = msg_id.encode() + b"." + timestamp.encode() + b"." + body
        digest = hmac.new(secret.encode(), signed, hashlib.sha256).digest()
        return "v1," + base64.b64encode(digest).decode()

    def test_accepts_standard_webhooks_wire_format(self):
        adapter = _make_adapter()
        msg_id = "msg_gitlab_123"
        ts = str(int(time.time()))
        sig = self._standard_signature(BODY, SECRET, msg_id, ts)
        req = _mock_request(headers={
            "webhook-id": msg_id,
            "webhook-timestamp": ts,
            "webhook-signature": sig,
        })
        assert adapter._validate_signature(req, BODY, SECRET, signature_mode="gitlab_standard") is True

    def test_rejects_under_github_mode(self):
        # Same headers must NOT validate when the route declares github.
        adapter = _make_adapter()
        msg_id = "msg_gitlab_123"
        ts = str(int(time.time()))
        sig = self._standard_signature(BODY, SECRET, msg_id, ts)
        req = _mock_request(headers={
            "webhook-id": msg_id,
            "webhook-timestamp": ts,
            "webhook-signature": sig,
        })
        assert adapter._validate_signature(req, BODY, SECRET, signature_mode="github") is False

    def test_wrong_body_rejects(self):
        adapter = _make_adapter()
        msg_id = "msg_gitlab_123"
        ts = str(int(time.time()))
        sig = self._standard_signature(BODY, SECRET, msg_id, ts)
        req = _mock_request(headers={
            "webhook-id": msg_id,
            "webhook-timestamp": ts,
            "webhook-signature": sig,
        })
        tampered = b'{"object_kind": "merge_request"}'
        assert adapter._validate_signature(req, tampered, SECRET, signature_mode="gitlab_standard") is False

    def test_stale_timestamp_rejects(self):
        adapter = _make_adapter()
        msg_id = "msg_gitlab_123"
        old_ts = str(int(time.time()) - 10000)
        sig = self._standard_signature(BODY, SECRET, msg_id, old_ts)
        req = _mock_request(headers={
            "webhook-id": msg_id,
            "webhook-timestamp": old_ts,
            "webhook-signature": sig,
        })
        assert adapter._validate_signature(req, BODY, SECRET, signature_mode="gitlab_standard") is False

    def test_legacy_token_does_not_validate_standard_mode(self):
        # X-Gitlab-Token (legacy plaintext) is NOT the standard-webhooks
        # wire format and must not validate under gitlab_standard.
        adapter = _make_adapter()
        req = _mock_request(headers={"X-Gitlab-Token": SECRET})
        assert adapter._validate_signature(req, BODY, SECRET, signature_mode="gitlab_standard") is False


class TestHindsightMode:
    """Hindsight signatures (issue #80327, fix by sg-shag in #80329).

    The wire contract: X-Hindsight-Signature carrying sha256=<hex> of the
    raw body — the same contract as GitHub, different header name.
    """

    def test_accepts_hindsight_wire_format(self):
        adapter = _make_adapter()
        sig = "sha256=" + hmac.new(
            SECRET.encode(), BODY, hashlib.sha256
        ).hexdigest()
        req = _mock_request(headers={"X-Hindsight-Signature": sig})
        assert adapter._validate_signature(req, BODY, SECRET, signature_mode="hindsight") is True

    def test_rejects_under_github_mode(self):
        adapter = _make_adapter()
        sig = "sha256=" + hmac.new(
            SECRET.encode(), BODY, hashlib.sha256
        ).hexdigest()
        req = _mock_request(headers={"X-Hindsight-Signature": sig})
        assert adapter._validate_signature(req, BODY, SECRET, signature_mode="github") is False

    def test_wrong_body_rejects(self):
        adapter = _make_adapter()
        sig = "sha256=" + hmac.new(
            SECRET.encode(), BODY, hashlib.sha256
        ).hexdigest()
        req = _mock_request(headers={"X-Hindsight-Signature": sig})
        tampered = b'{"event": "delete"}'
        assert adapter._validate_signature(req, tampered, SECRET, signature_mode="hindsight") is False

    def test_github_header_does_not_validate_hindsight_mode(self):
        adapter = _make_adapter()
        req = _mock_request(headers={"X-Hub-Signature-256": _github_signature(BODY, SECRET)})
        assert adapter._validate_signature(req, BODY, SECRET, signature_mode="hindsight") is False
