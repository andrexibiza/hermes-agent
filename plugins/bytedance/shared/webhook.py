"""Shared webhook ingress shell.

Per the design spec §7.3, each provider supplies a verifier and parser.
The ingress supplies the invariant shell:

1. Match exact route and account binding.
2. Read no more than the configured byte limit.
3. Verify signature/challenge against raw bytes before JSON mutation.
4. Parse UTF-8 JSON and require a top-level object.
5. Extract provider event ID and account binding.
6. Atomically insert the composite idempotency key.
7. Return the provider-required acknowledgment immediately.
8. Queue normalized processing by conversation.
9. Record processing success/failure separately from acknowledgment.

Per §3.2 (Webhook Revolution baseline):
- Composite idempotency key: (profile, route, provider, account_alias, event_id)
- Rate limiting is isolated by profile and route, then provider/account
- JSON arrays and scalars are rejected as invalid webhook envelopes
- Body length is measured in UTF-8 bytes
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Protocol, Tuple

from plugins.bytedance.shared.errors import ProviderError
from plugins.bytedance.shared.observability import Metrics
from plugins.bytedance.shared.rate_limit import RateLimiter
from plugins.bytedance.shared.state import StateStore, get_state_store

logger = logging.getLogger(__name__)

# Default limits from the design spec
DEFAULT_MAX_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB
DEFAULT_QUEUE_CAPACITY = 1_000
DEFAULT_MAX_CONCURRENT_CONVERSATIONS = 32


class WebhookVerifier(Protocol):
    """Provider-supplied signature/challenge verifier.

    Implementations verify the raw bytes against the provider's
    signature algorithm.  Must be deterministic and fail-closed
    on any parsing error.
    """

    def verify(self, raw_body: bytes, headers: Dict[str, str], route_config: dict) -> Tuple[bool, Optional[str]]:
        """Return (True, None) if valid, (False, reason) otherwise."""
        ...


class WebhookParser(Protocol):
    """Provider-supplied event parser.

    Implementations extract the normalized event fields from the
    parsed JSON payload AFTER signature verification.
    """

    def parse(self, payload: dict, route_config: dict) -> "NormalizedEvent":
        """Parse the provider payload into a normalized event."""
        ...


@dataclass(frozen=True)
class NormalizedEvent:
    """Normalized inbound event envelope (design spec §6.2).

    The raw body is NOT persisted.  A SHA-256 digest is retained
    for duplicate and forensic correlation.
    """

    schema_version: int
    provider: str
    profile: str
    account_alias: str
    event_id: str
    event_type: str
    occurred_at: Optional[float]  # epoch seconds
    received_at: float
    conversation_id: Optional[str]
    message_id: Optional[str]
    sender_id: Optional[str]
    recipient_id: Optional[str]
    message_type: Optional[str]
    payload: dict
    raw_sha256: str


class CompositeIdempotencyKey:
    """Composite idempotency key: (profile, route, provider, account_alias, event_id).

    Fallback only when no provider event ID exists:
    (profile, route, provider, account_alias, sha256(raw_bytes), received_time_bucket)
    """

    @staticmethod
    def build(
        profile: str,
        route: str,
        provider: str,
        account_alias: str,
        event_id: str,
    ) -> Tuple[str, str, str, str, str]:
        return (profile, route, provider, account_alias, event_id)

    @staticmethod
    def build_fallback(
        profile: str,
        route: str,
        provider: str,
        account_alias: str,
        raw_body: bytes,
        received_at: float,
        time_bucket_seconds: int = 60,
    ) -> Tuple[str, str, str, str, str]:
        """Fallback key when no provider event ID exists.

        A fallback key must never be used where a stable provider
        message/event ID exists.
        """
        digest = hashlib.sha256(raw_body).hexdigest()
        time_bucket = str(int(received_at / time_bucket_seconds))
        # Combine digest + time_bucket into the event_id slot
        combined = f"{digest}:{time_bucket}"
        return (profile, route, provider, account_alias, combined)


class WebhookIngress:
    """Shared webhook ingress shell.

    Each provider supplies a ``WebhookVerifier`` and ``WebhookParser``.
    This shell handles the invariant lifecycle: byte limits, signature
    verification, JSON validation, composite idempotency, and
    acknowledgment.
    """

    def __init__(
        self,
        provider: str,
        verifier: WebhookVerifier,
        parser: WebhookParser,
        *,
        state_store: Optional[StateStore] = None,
        rate_limiter: Optional[RateLimiter] = None,
        max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    ) -> None:
        self.provider = provider
        self._verifier = verifier
        self._parser = parser
        self._state = state_store or get_state_store()
        self._rate_limiter = rate_limiter or RateLimiter(
            window_seconds=60.0, max_requests=30
        )
        self._max_body_bytes = max_body_bytes

    def verify_and_parse(
        self,
        raw_body: bytes,
        headers: Dict[str, str],
        route_config: dict,
        *,
        profile: str,
        account_alias: str,
        route: str,
    ) -> Tuple[Optional[NormalizedEvent], Optional[str]]:
        """Verify signature and parse a webhook body.

        Returns (event, error).  Exactly one is non-None.

        The composite idempotency key is inserted atomically before
        this method returns.  A duplicate returns a sentinel
        indicating "duplicate — ack is fine, no dispatch needed".
        """
        now = time.time()

        # 2. Check byte limit BEFORE signature verification (auth-before-body)
        if len(raw_body) > self._max_body_bytes:
            Metrics.increment(
                "bytedance_webhook_rejected_total",
                labels={"provider": self.provider, "reason": "oversized_body"},
            )
            return None, f"Body exceeds {self._max_body_bytes} byte limit"

        # 3. Verify signature against raw bytes
        if not self._verifier.verify(raw_body, headers, route_config):
            Metrics.increment(
                "bytedance_webhook_rejected_total",
                labels={"provider": self.provider, "reason": "bad_signature"},
            )
            return None, "Signature verification failed"

        # 4. Parse UTF-8 JSON and require top-level object
        try:
            text = raw_body.decode("utf-8")
        except UnicodeDecodeError:
            Metrics.increment(
                "bytedance_webhook_rejected_total",
                labels={"provider": self.provider, "reason": "invalid_utf8"},
            )
            return None, "Body is not valid UTF-8"

        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            Metrics.increment(
                "bytedance_webhook_rejected_total",
                labels={"provider": self.provider, "reason": "invalid_json"},
            )
            return None, "Body is not valid JSON"

        # Reject arrays and scalars — require top-level object
        if not isinstance(payload, dict):
            Metrics.increment(
                "bytedance_webhook_rejected_total",
                labels={"provider": self.provider, "reason": "non_object_json"},
            )
            return None, "Webhook payload must be a JSON object, not array or scalar"

        # 5. Parse into normalized event
        try:
            event = self._parser.parse(payload, route_config)
        except ProviderError as e:
            return None, f"Parser error: {e}"

        raw_sha256 = hashlib.sha256(raw_body).hexdigest()

        # 6. Atomically insert composite idempotency key
        # Use the event_id from the parser, or build a fallback
        if event.event_id:
            key_parts = CompositeIdempotencyKey.build(
                profile, route, self.provider, account_alias, event.event_id
            )
        else:
            key_parts = CompositeIdempotencyKey.build_fallback(
                profile, route, self.provider, account_alias, raw_body, now
            )

        profile_key, route_key, provider_key, account_key, id_key = key_parts

        inserted = self._state.insert_webhook_event(
            profile_key,
            route_key,
            provider_key,
            account_key,
            id_key,
            raw_sha256=raw_sha256,
        )

        if not inserted:
            Metrics.increment(
                "bytedance_webhook_duplicate_total",
                labels={"provider": self.provider, "account": account_alias},
            )
            return None, "__DUPLICATE__"

        Metrics.increment(
            "bytedance_webhook_received_total",
            labels={"provider": self.provider, "account": account_alias, "event_type": event.event_type},
        )
        return event, None

    def is_duplicate_sentinel(self, error: Optional[str]) -> bool:
        """Check if the error indicates a duplicate (not a real error)."""
        return error == "__DUPLICATE__"

    def acknowledge(self, route_config: dict) -> Dict[str, Any]:
        """Return the provider-required acknowledgment payload.

        Providers typically expect a 200 with either an echo of the
        challenge token or a simple status object.  The exact format
        is provider-specific and determined by the route_config.
        """
        challenge = route_config.get("challenge", "")
        if challenge:
            return {"challenge": challenge, "status": "ok"}
        return {"status": "ok"}
