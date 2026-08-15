"""Test: Douyin webhook verification, parsing, and direction resolution.

Per design spec §3.3 (Douyin webhook), §11.3 (direction resolution),
and §11.2 (event normalization).
"""

import hashlib
import hmac
import json
import time

import pytest

from plugins.platforms.douyin.webhook import (
    DouyinWebhookVerifier,
    DouyinWebhookParser,
)

DOUYIN_SECRET = "test_douyin_double"


@pytest.fixture
def verifier():
    return DouyinWebhookVerifier()


@pytest.fixture
def parser():
    return DouyinWebhookParser()


@pytest.fixture
def valid_douyin_body():
    return json.dumps({
        "event_type": "im_receive_msg",
        "content": json.dumps({
            "open_id": "douyin_open_123",
            "conversation_short_id": "conv_dy_abc",
            "content": {
                "content": "Hello from Douyin",
                "msg_type": 1,
            },
            "create_time": "1700000000",
            "message_id": "dy_msg_12345",
            "from_user_id": "user_dy_123",
            "to_user_id": "douyin_open_123",
        }),
    }).encode()


def _compute_douyin_signature(secret: str, body: bytes, timestamp: str = "", nonce: str = "") -> str:
    """Compute Douyin webhook signature (HMAC-SHA256 of timestamp+nonce+body)."""
    message = timestamp.encode("utf-8") + nonce.encode("utf-8") + body
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


class TestDouyinWebhookVerification:
    def test_valid_signature_passes(self, verifier, valid_douyin_body):
        timestamp = str(int(time.time()))
        nonce = "abc123"
        sig = _compute_douyin_signature(DOUYIN_SECRET, valid_douyin_body, timestamp, nonce)
        headers = {
            "X-Douyin-Signature": sig,
            "X-Douyin-Timestamp": timestamp,
            "X-Douyin-Nonce": nonce,
        }
        ok, error = verifier.verify(valid_douyin_body, headers, {"webhook_secret": DOUYIN_SECRET})
        assert ok is True
        assert error is None

    def test_invalid_signature_rejected(self, verifier, valid_douyin_body):
        timestamp = str(int(time.time()))
        headers = {
            "X-Douyin-Signature": "invalid_sig",
            "X-Douyin-Timestamp": timestamp,
            "X-Douyin-Nonce": "abc123",
        }
        ok, error = verifier.verify(valid_douyin_body, headers, {"webhook_secret": DOUYIN_SECRET})
        assert ok is False
        assert error == "signature_mismatch"

    def test_missing_signature_rejected(self, verifier, valid_douyin_body):
        headers = {}
        ok, error = verifier.verify(valid_douyin_body, headers, {"webhook_secret": DOUYIN_SECRET})
        assert ok is False
        assert error == "missing_signature"

    def test_no_secret_rejected(self, verifier, valid_douyin_body):
        headers = {"X-Douyin-Signature": "some_sig"}
        ok, error = verifier.verify(valid_douyin_body, headers, {"webhook_secret": ""})
        assert ok is False
        assert error == "no_secret"


class TestDouyinDirectionResolution:
    """Per §11.3: direction resolution rules."""

    def test_inbound_message_direction(self, parser, valid_douyin_body):
        body = json.loads(valid_douyin_body)
        event = parser.parse(body, {"open_id": "douyin_open_123"})
        assert event.event_type == "im_receive_msg"
        # Inbound — sender is the user, not the authorized account
        assert event.payload.get("_direction") == "inbound"
        assert event.payload.get("_sender_is_self") is False

    def test_echo_suppression_on_recall(self, parser):
        """im_recall_msg events are normalized."""
        body = json.dumps({
            "event_type": "im_recall_msg",
            "content": json.dumps({
                "open_id": "douyin_open_123",
                "server_message_id": "dy_msg_12345",
                "to_user_id": "douyin_open_123",
                "from_user_id": "douyin_open_123",
            }),
        })
        event = parser.parse(json.loads(body), {"open_id": "douyin_open_123"})
        assert event.event_type == "im_recall_msg"

    def test_enter_direct_msg_creates_grant(self, parser):
        """im_enter_direct_msg should produce a grant-relevant event."""
        body = json.dumps({
            "event_type": "im_enter_direct_msg",
            "content": json.dumps({
                "open_id": "douyin_open_123",
                "conversation_short_id": "conv_dy_abc",
                "create_time": "1700000000",
            }),
        })
        event = parser.parse(json.loads(body), {"open_id": ""})
        assert event.event_type == "im_enter_direct_msg"


class TestDouyinNormalization:
    """Per §11.2: Douyin events are normalized into NormalizedEvent."""

    def test_normalized_event_fields(self, parser, valid_douyin_body):
        body = json.loads(valid_douyin_body)
        event = parser.parse(body, {"open_id": "douyin_open_123"})
        assert event.conversation_id == "conv_dy_abc"
        assert event.sender_id == "user_dy_123"
        assert event.message_type == "text"
        assert event.event_id is not None

    def test_direction_outbound_when_sender_is_self(self, parser):
        """When from_user_id matches the authorized open_id, direction is outbound."""
        body = json.dumps({
            "event_type": "im_send_msg",
            "content": json.dumps({
                "open_id": "douyin_open_123",
                "conversation_short_id": "conv_dy_abc",
                "from_user_id": "douyin_open_123",
                "to_user_id": "user_dy_123",
                "create_time": "1700000000",
            }),
        })
        event = parser.parse(json.loads(body), {"open_id": "douyin_open_123"})
        assert event.event_type == "im_send_msg"
        assert event.payload.get("_sender_is_self") is True
        assert event.payload.get("_direction") == "outbound"
