"""Test: TikTok Business webhook verification and parsing.

Per design spec §7.3 and BD-16: verifies that the TikTok Business
webhook verification (HMAC-SHA256) and event parsing work correctly.
"""

import hashlib
import hmac
import json

import pytest

from plugins.platforms.tiktok_business.webhook import (
    TikTokWebhookVerifier,
    TikTokWebhookParser,
)


@pytest.fixture
def verifier():
    return TikTokWebhookVerifier(require_timestamp=False)


@pytest.fixture
def parser():
    return TikTokWebhookParser()


@pytest.fixture
def secret():
    return "test_secret_12345"


@pytest.fixture
def valid_body():
    return json.dumps({
        "webhook_type": "message",
        "data": {
            "message_id": "msg_12345",
            "conversation_id": "conv_abc",
            "content": "Hello from TikTok",
            "sender_id": "user_tiktok_123",
            "create_time": 1700000000,
            "msg_type": "text",
        },
    }).encode()


def _compute_signature(secret: str, body: bytes) -> str:
    """Compute TikTok Business webhook signature (HMAC-SHA256)."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class TestTikTokWebhookVerification:
    def test_valid_signature_passes(self, verifier, secret, valid_body):
        signature = _compute_signature(secret, valid_body)
        headers = {"X-TikTok-Signature": signature}
        ok, error = verifier.verify(
            valid_body, headers, {"webhook_secret": secret}
        )
        assert ok is True
        assert error is None

    def test_invalid_signature_rejected(self, verifier, secret, valid_body):
        headers = {"X-TikTok-Signature": "invalid_signature"}
        ok, error = verifier.verify(
            valid_body, headers, {"webhook_secret": secret}
        )
        assert ok is False
        assert error == "signature_mismatch"

    def test_missing_signature_rejected(self, verifier, secret, valid_body):
        headers = {}
        ok, error = verifier.verify(
            valid_body, headers, {"webhook_secret": secret}
        )
        assert ok is False
        assert error == "missing_signature"

    def test_empty_body_no_signature_rejected(self, verifier, secret):
        # Empty body with no signature — fail closed
        headers = {}
        ok, error = verifier.verify(
            b"", headers, {"webhook_secret": secret},
        )
        assert ok is False

    def test_no_secret_rejected(self, verifier, valid_body):
        headers = {"X-TikTok-Signature": "some_sig"}
        ok, error = verifier.verify(
            valid_body, headers, {"webhook_secret": ""},
        )
        assert ok is False


class TestTikTokWebhookParsing:
    def test_parse_text_message(self, parser, valid_body):
        body = json.loads(valid_body)
        event = parser.parse(body, {"account_open_id": ""})
        assert event.event_type == "message"
        assert event.conversation_id == "conv_abc"
        assert event.message_type == "text"
        assert event.payload["content"] == "Hello from TikTok"


class TestIdempotencyKey:
    def test_composite_key_is_deterministic(self):
        from plugins.bytedance.shared.webhook import CompositeIdempotencyKey

        key1 = CompositeIdempotencyKey.build(
            profile="default",
            route="route_1",
            provider="tiktok_business",
            account_alias="biz_1",
            event_id="event_123",
        )
        key2 = CompositeIdempotencyKey.build(
            profile="default",
            route="route_1",
            provider="tiktok_business",
            account_alias="biz_1",
            event_id="event_123",
        )
        assert key1 == key2

    def test_different_event_id_different_key(self):
        from plugins.bytedance.shared.webhook import CompositeIdempotencyKey

        key1 = CompositeIdempotencyKey.build(
            profile="p", route="r", provider="prov", account_alias="a", event_id="e1",
        )
        key2 = CompositeIdempotencyKey.build(
            profile="p", route="r", provider="prov", account_alias="a", event_id="e2",
        )
        assert key1 != key2
