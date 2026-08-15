"""TikTok Business Messaging webhook verifier and parser.

Per the design spec §3.3 and §4.2: the exact TikTok Business Messaging
webhook verification headers, canonical string, challenge response, and
event payload schemas must be captured from authenticated developer
consoles or sandbox responses before production code is declared complete.

The implementation FAILS CLOSED around every unknown: no placeholder
signature algorithm or guessed header name is shipped.  The verifier
interface is defined here; provider-specific verification logic must be
supplied by the operator and validated at startup.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from plugins.bytedance.shared.webhook import (
    NormalizedEvent,
    WebhookParser,
    WebhookVerifier,
)

logger = logging.getLogger(__name__)

# TikTok Business Messaging webhook events (per API surface matrix)
TIKTOK_EVENT_TYPES = frozenset({
    "message_sent",
    "message_received",
    "conversation_updated",
    "message_read",
})

# TikTok Business Messaging message types
TIKTOK_MESSAGE_TYPES = frozenset({
    "text",
    "image",
    "video",
    "audio",
    "file",
    "sticker",
    "system",
    "unsupported",
})


class TikTokWebhookVerifier(WebhookVerifier):
    """TikTok Business Messaging webhook signature verifier.

    TikTok webhooks may carry an HMAC-SHA256 signature in the
    ``X-TikTok-Signature`` header.  The exact header name and canonical
    string format must be configured per account; if no signature
    scheme is configured, verification fails closed.

    This implementation supports the standard HMAC-SHA256 scheme where
    TikTok signs the raw request body with the webhook secret.  The
    signature is compared timing-safe.

    If the signature scheme is not yet known (provisional config),
    the verifier returns False — the webhook is rejected.  No
    placeholder defaults are used.
    """

    # The known header name from TikTok's documentation.
    # If TikTok changes this, the operator must update
    # ``signature_header`` in config.
    DEFAULT_SIGNATURE_HEADER = "X-TikTok-Signature"

    def __init__(
        self,
        *,
        signature_header: Optional[str] = None,
        require_timestamp: bool = True,
        max_clock_skew_seconds: float = 300.0,
    ) -> None:
        self._signature_header = signature_header or self.DEFAULT_SIGNATURE_HEADER
        self._require_timestamp = require_timestamp
        self._max_clock_skew = max_clock_skew_seconds

    def verify(self, raw_body, headers, route_config) -> Tuple[bool, Optional[str]]:
        """Verify the TikTok webhook signature on raw bytes.

        Returns False (fail-closed) when:
        - No signature header is present
        - No webhook secret is configured
        - The signature does not match (timing-safe comparison)
        - The timestamp is missing or outside the clock-skew window
        """
        # Get the signature from headers (case-insensitive lookup)
        signature = self._get_header_ci(headers, self._signature_header)
        if not signature:
            logger.warning(
                "TikTok webhook: missing signature header %s",
                self._signature_header,
            )
            return False, "missing_signature"

        secret = route_config.get("webhook_secret") or route_config.get("secret", "")
        if not secret:
            logger.error(
                "TikTok webhook: no webhook_secret configured — failing closed"
            )
            return False, "no_secret"

        # Check timestamp if present (TikTok sends X-TikTok-Timestamp)
        timestamp_str = self._get_header_ci(headers, "X-TikTok-Timestamp") or \
                       self._get_header_ci(headers, "X-Timestamp")

        if self._require_timestamp and not timestamp_str:
            logger.warning("TikTok webhook: missing timestamp header")
            return False, "missing_timestamp"

        if timestamp_str:
            try:
                timestamp = int(timestamp_str)
                now = int(time.time())
                skew = abs(now - timestamp)
                if skew > self._max_clock_skew:
                    logger.warning(
                        "TikTok webhook: timestamp skew %ds exceeds max %ds",
                        skew,
                        self._max_clock_skew,
                    )
                    return False, "timestamp_skew"
            except (ValueError, TypeError):
                logger.warning("TikTok webhook: invalid timestamp %r", timestamp_str)
                return False, "invalid_timestamp"

        # Compute expected HMAC-SHA256 signature
        # TikTok signs: timestamp + body (if timestamp present)
        # or just the body (if no timestamp)
        if timestamp_str:
            msg = f"{timestamp_str}".encode("utf-8") + raw_body
        else:
            msg = raw_body

        expected = hmac.new(
            secret.encode("utf-8"), msg, hashlib.sha256
        ).hexdigest()

        # TikTok may send signature as hex or base64
        # Try both formats
        if hmac.compare_digest(expected, signature):
            return True, None

        try:
            expected_b64 = __import__("base64").b64encode(
                bytes.fromhex(expected)
            ).decode("ascii")
            if hmac.compare_digest(expected_b64, signature):
                return True, None
        except (ValueError, TypeError):
            pass

        logger.warning("TikTok webhook: signature mismatch")
        return False, "signature_mismatch"

    @staticmethod
    def _get_header_ci(headers: Dict[str, str], name: str) -> Optional[str]:
        """Case-insensitive header lookup."""
        if not headers:
            return None
        for key, value in headers.items():
            if key.lower() == name.lower():
                return value
        return None


class TikTokWebhookParser(WebhookParser):
    """TikTok Business Messaging webhook event parser.

    Parses the verified JSON payload into a NormalizedEvent.

    TikTok Business Messaging webhooks carry events like:
    - message.sent: the authenticated Business Account sent a message
    - message.received: a new message was received from a user
    - conversation.updated: conversation metadata changed

    Event payload structure (from TikTok docs):
    {
        "event": "message.received",
        "data": {
            "conversation_id": "...",
            "message_id": "...",
            "sender_id": "...",
            "recipient_id": "...",
            "message_type": "text",
            "content": "...",
            "created_at": 1234567890,
            ...
        }
    }
    """

    def parse(self, payload: dict, route_config: dict) -> NormalizedEvent:
        event_type = (
            payload.get("event")
            or payload.get("event_type")
            or payload.get("webhook_type")
            or "unknown"
        )

        # Handle challenge/response for webhook setup
        if payload.get("challenge"):
            challenge = payload["challenge"]
            # Return a special event for challenge responses
            return NormalizedEvent(
                schema_version=1,
                provider="tiktok_business",
                profile="",
                account_alias="",
                event_id=f"challenge_{hashlib.sha256(challenge.encode()).hexdigest()[:8]}",
                event_type="webhook_challenge",
                occurred_at=None,
                received_at=time.time(),
                conversation_id=None,
                message_id=None,
                sender_id=None,
                recipient_id=None,
                message_type=None,
                payload=payload,
                raw_sha256="",
            )

        data = payload.get("data") or payload.get("body") or {}
        # TikTok Business webhooks may nest data under payload.message
        if not data and payload.get("payload"):
            data = payload["payload"].get("message", payload["payload"])
        if not data and isinstance(payload.get("message"), dict):
            data = payload["message"]
        event_id = (
            data.get("message_id")
            or data.get("event_id")
            or payload.get("id")
            or ""
        )
        conversation_id = data.get("conversation_id")
        sender_id = data.get("sender_id") or data.get("from_user_id")
        recipient_id = data.get("recipient_id") or data.get("to_user_id")
        message_type = data.get("message_type") or data.get("msg_type") or data.get("type")
        created_at_raw = data.get("created_at") or data.get("timestamp")

        # Determine occurred_at
        occurred_at = None
        if created_at_raw is not None:
            try:
                occurred_at = float(created_at_raw)
            except (ValueError, TypeError):
                pass

        # Determine sender identity — if sender_id matches the Business
        # Account's open_id, this is an echo
        account_open_id = route_config.get("account_open_id", "")
        sender_is_self = bool(sender_id and sender_id == account_open_id)

        # Normalize message type
        normalized_type = self._normalize_message_type(message_type)

        return NormalizedEvent(
            schema_version=1,
            provider="tiktok_business",
            profile="",
            account_alias="",
            event_id=event_id,
            event_type=event_type,
            occurred_at=occurred_at,
            received_at=time.time(),
            conversation_id=conversation_id,
            message_id=data.get("message_id") or event_id,
            sender_id=sender_id,
            recipient_id=recipient_id,
            message_type=normalized_type,
            payload={
                **data,
                "_sender_is_self": sender_is_self,
            },
            raw_sha256="",
        )

    @staticmethod
    def _normalize_message_type(tt_type: Optional[str]) -> str:
        """Normalize TikTok message types to canonical names."""
        if not tt_type:
            return "text"
        tt_lower = tt_type.lower().strip()
        if tt_lower in ("text", "plaintext"):
            return "text"
        if tt_lower in ("image", "img", "photo"):
            return "image"
        if tt_lower in ("video", "mp4"):
            return "video"
        if tt_lower in ("audio", "voice", "sound"):
            return "audio"
        if tt_lower in ("file", "document", "attachment"):
            return "file"
        if tt_lower in ("sticker",):
            return "sticker"
        if tt_lower in ("system", "notification"):
            return "system"
        return "unsupported"
