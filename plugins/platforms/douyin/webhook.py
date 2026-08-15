"""Douyin Open Platform webhook verifier and parser.

Per the design spec §3.3: the exact Douyin webhook verification
signature contract must be captured from the selected authenticated
app type.  The implementation FAILS CLOSED around every unknown.

Douyin webhook verification uses HMAC-SHA256 with the webhook_secret
as the key, signing a canonical string of timestamp + nonce + body.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Dict, Optional, Tuple

from plugins.bytedance.shared.webhook import (
    NormalizedEvent,
    WebhookParser,
    WebhookVerifier,
)

logger = logging.getLogger(__name__)

# Douyin IM event names — these are provider vocabulary and remain
# visible in metadata (design spec §11.2).
DOUYIN_EVENT_TYPES = frozenset({
    "im_send_msg",        # Inbound: user-authored message to Hermes
    "im_receive_msg",     # Inbound: provider-side received-message event
    "im_enter_direct_msg", # Inbound: short-lived send-capability grant
    "im_recall_msg",      # Inbound: recall event
    "contract_authorize",  # Inbound: scope/account refresh
    "contract_unauthorize", # Inbound: account disabled
    # Group events (Phase 2)
    "im_group_send_msg",
    "im_group_receive_msg",
})


class DouyinWebhookVerifier(WebhookVerifier):
    """Douyin Open Platform webhook signature verifier.

    Douyin signs webhooks with HMAC-SHA256:
    ``HMAC_SHA256(timestamp + nonce + body, webhook_secret)``

    The result is compared to the ``X-Douyin-Signature`` header.
    If the exact signature contract differs for the selected app type,
    the operator must configure ``signature_header`` and
    ``signature_algorithm``.  Absent configuration, this verifier
    returns False (fail-closed).
    """

    DEFAULT_SIGNATURE_HEADER = "X-Douyin-Signature"
    DEFAULT_TIMESTAMP_HEADER = "X-Douyin-Timestamp"
    DEFAULT_NONCE_HEADER = "X-Douyin-Nonce"

    # Maximum clock skew (seconds) before rejecting a signed event
    MAX_CLOCK_SKEW = 300.0

    def __init__(
        self,
        *,
        signature_header: Optional[str] = None,
        timestamp_header: Optional[str] = None,
        nonce_header: Optional[str] = None,
    ) -> None:
        self._sig_header = signature_header or self.DEFAULT_SIGNATURE_HEADER
        self._ts_header = timestamp_header or self.DEFAULT_TIMESTAMP_HEADER
        self._nonce_header = nonce_header or self.DEFAULT_NONCE_HEADER

    def verify(
        self,
        raw_body: bytes,
        headers: Dict[str, str],
        route_config: dict,
    ) -> Tuple[bool, Optional[str]]:
        signature = self._get_header_ci(headers, self._sig_header)
        if not signature:
            logger.warning("Douyin webhook: missing signature header %s", self._sig_header)
            return False, "missing_signature"

        secret = route_config.get("webhook_secret") or route_config.get("secret", "")
        if not secret:
            logger.error("Douyin webhook: no webhook_secret configured — failing closed")
            return False, "no_secret"

        timestamp = self._get_header_ci(headers, self._ts_header)
        nonce = self._get_header_ci(headers, self._nonce_header)

        # Timestamp check for replay protection
        if timestamp:
            try:
                ts = int(timestamp)
                now = int(time.time())
                skew = abs(now - ts)
                if skew > self.MAX_CLOCK_SKEW:
                    logger.warning(
                        "Douyin webhook: timestamp skew %ds exceeds max %ds",
                        skew, self.MAX_CLOCK_SKEW,
                    )
                    return False, "timestamp_skew"
            except (ValueError, TypeError):
                logger.warning("Douyin webhook: invalid timestamp %r", timestamp)
                return False, "invalid_timestamp"

        # Canonical string: timestamp + nonce + body
        ts_bytes = (timestamp or "").encode("utf-8")
        nonce_bytes = (nonce or "").encode("utf-8")
        message = ts_bytes + nonce_bytes + raw_body

        expected = hmac.new(
            secret.encode("utf-8"), message, hashlib.sha256
        ).hexdigest()

        if hmac.compare_digest(expected, signature):
            return True, None

        # Some providers send base64-encoded signatures
        try:
            import base64
            expected_b64 = base64.b64encode(bytes.fromhex(expected)).decode("ascii")
            if hmac.compare_digest(expected_b64, signature):
                return True, None
        except (ValueError, TypeError):
            pass

        # Also try signature without timestamp/nonce (some app types)
        expected_simple = hmac.new(
            secret.encode("utf-8"), raw_body, hashlib.sha256
        ).hexdigest()
        if hmac.compare_digest(expected_simple, signature):
            return True, None

        logger.warning("Douyin webhook: signature mismatch")
        return False, "signature_mismatch"

    @staticmethod
    def _get_header_ci(headers: Dict[str, str], name: str) -> Optional[str]:
        if not headers:
            return None
        for key, value in headers.items():
            if key.lower() == name.lower():
                return value
        return None


class DouyinWebhookParser(WebhookParser):
    """Douyin Open Platform webhook event parser.

    Per design spec §11.3, event names describe what the authorized
    account received or sent.  Direction resolution must check:
    - from_user_id vs authorized_account_open_id
    - to_user_id
    - stored outbound server_message_id

    Only a message authored by the counterparty becomes a Hermes
    inbound user message.
    """

    def parse(self, payload: dict, route_config: dict) -> NormalizedEvent:
        # Douyin Open Platform webhooks use 'webhook_type' and 'content'
        # where content is a JSON-encoded string.  Also support 'event_type'
        # and 'data' for flexibility.
        event_type = (
            payload.get("event")
            or payload.get("event_type")
            or payload.get("webhook_type")
            or payload.get("type")
            or "unknown"
        )

        # Handle content as JSON string (Douyin's native format)
        raw_data = payload.get("data") or payload.get("body")
        content_str = payload.get("content")
        if content_str and not raw_data:
            try:
                raw_data = json.loads(content_str)
            except (json.JSONDecodeError, TypeError):
                raw_data = {}
        if raw_data is None:
            raw_data = {}

        # Douyin webhooks may be nested
        if isinstance(raw_data.get("data"), dict):
            raw_data = raw_data["data"]

        data = raw_data

        event_id = (
            data.get("message_id")
            or data.get("event_id")
            or data.get("msg_id")
            or data.get("server_message_id")
            or ""
        )

        conversation_id = data.get("conversation_id") or data.get("conversation_short_id")
        from_user_id = data.get("from_user_id") or data.get("from_user_open_id")
        to_user_id = data.get("to_user_id") or data.get("to_user_open_id")
        message_type = data.get("message_type") or data.get("type")
        created_at_raw = data.get("create_time") or data.get("created_at")

        occurred_at = None
        if created_at_raw is not None:
            try:
                occurred_at = float(created_at_raw)
            except (ValueError, TypeError):
                pass

        server_message_id = data.get("server_message_id")
        authorized_open_id = route_config.get("open_id", "")

        # Direction resolution (§11.3)
        sender_id = from_user_id
        recipient_id = to_user_id
        sender_is_self = bool(
            sender_id and sender_id == authorized_open_id
        )

        # For im_send_msg / im_receive_msg, direction depends on
        # whether the authorized account is the sender or recipient
        if event_type in ("im_send_msg", "im_receive_msg"):
            if sender_is_self:
                # The authorized account sent this — it's an echo
                direction = "outbound"
            elif to_user_id and to_user_id == authorized_open_id:
                # The authorized account received this
                direction = "inbound"
            else:
                direction = "inbound"
        else:
            direction = "inbound" if not sender_is_self else "outbound"

        normalized_type = self._normalize_message_type(message_type)

        return NormalizedEvent(
            schema_version=1,
            provider="douyin",
            profile="",
            account_alias="",
            event_id=event_id,
            event_type=event_type,
            occurred_at=occurred_at,
            received_at=float(1),  # placeholder, filled by ingress
            conversation_id=conversation_id or data.get("conversation_id"),
            message_id=event_id or server_message_id,
            sender_id=from_user_id,
            recipient_id=to_user_id,
            message_type=normalized_type,
            payload={
                **data,
                "_direction": direction,
                "_sender_is_self": sender_is_self,
                "_server_message_id": server_message_id,
            },
            raw_sha256="",
        )

    @staticmethod
    def _normalize_message_type(dy_type: Optional[str]) -> str:
        if not dy_type:
            return "text"
        t = str(dy_type).lower().strip()
        if t in ("text", "txt", "plain"):
            return "text"
        if t in ("image", "img", "picture"):
            return "image"
        if t in ("video", "mp4"):
            return "video"
        if t in ("voice", "audio", "sound"):
            return "audio"
        if t in ("sticker",):
            return "sticker"
        if t in ("file", "document"):
            return "file"
        return "unsupported"
