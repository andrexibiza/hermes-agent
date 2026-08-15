"""Shared test fixtures for hermes-bytedance."""

import json
import os
import tempfile
from pathlib import Path


FIXTURES_DIR = Path(__file__).parent


def load_fixture(name: str) -> dict:
    """Load a JSON fixture file."""
    path = FIXTURES_DIR / name
    with open(path) as f:
        return json.load(f)


# Re-export common fixtures
def tiktok_business_event_fixture() -> dict:
    """A sample TikTok Business Messaging webhook event."""
    return {
        "payload": {
            "webhook_type": "message",
            "message": {
                "message_id": "msg_12345",
                "conversation_id": "conv_abc",
                "content": "Hello from TikTok",
                "sender_id": "user_tiktok_123",
                "create_time": 1700000000,
                "msg_type": "text",
            },
        },
        "headers": {
            "X-Tt-Drm-Verify": "tiktok_signature_here",
        },
    }


def douyin_im_event_fixture() -> dict:
    """A sample Douyin IM webhook event (text message)."""
    return {
        "webhook_type": "im_receive_msg",
        "content": json.dumps({
            "open_id": "douyin_open_123",
            "conversation_short_id": "conv_dy_abc",
            "content": {
                "content": "Hello from Douyin",
                "msg_type": 1,
            },
            "create_time": "1700000000",
            "message_id": "dy_msg_12345",
            "sender": {"from_id": "user_dy_123"},
        }),
    }


def douyin_enter_direct_msg_fixture() -> dict:
    """A sample Douyin im_enter_direct_msg event."""
    return {
        "webhook_type": "im_enter_direct_msg",
        "content": json.dumps({
            "open_id": "douyin_open_123",
            "conversation_short_id": "conv_dy_abc",
            "create_time": "1700000000",
        }),
    }


def douyin_recall_msg_fixture() -> dict:
    """A sample Douyin im_recall_msg event."""
    return {
        "webhook_type": "im_recall_msg",
        "content": json.dumps({
            "open_id": "douyin_open_123",
            "server_message_id": "dy_msg_12345",
            "to_user_id": "to_user_123",
        }),
    }


def tiktok_token_info_fixture() -> dict:
    """Sample TikTok token introspection response."""
    return {
        "data": {
            "access_token": "tk_test_token",
            "expires_in": 7200,
            "scope": [
                "message.info",
                "message.send",
                "video.list",
                "video.create",
                "comment.list",
                "comment.reply",
            ],
        },
    }


def douyin_token_info_fixture() -> dict:
    """Sample Douyin token introspection response."""
    return {
        "data": {
            "access_token": "dy_test_token",
            "expires_in": 7200,
            "scope": [
                "im.direct_message",
                "video.list",
            ],
        },
    }
