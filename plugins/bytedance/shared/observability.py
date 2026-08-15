"""Observability: redacted metrics and structured logging.

Per the design spec §7.6: logs use hashed or truncated identifiers.
Tokens, signatures, raw headers, and message bodies are excluded
from logs by default.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("hermes_bytedance")


def hash_id(identifier: str, length: int = 12) -> str:
    """Hash an identifier for redacted logging.

    Returns the first ``length`` hex characters of its SHA-256 digest.
    This lets operators correlate log entries without exposing the raw
    provider conversation ID, user ID, or message ID.
    """
    if identifier is None:
        return "none"
    return hashlib.sha256(str(identifier).encode("utf-8")).hexdigest()[:length]


def truncate_id(identifier: str, length: int = 16) -> str:
    """Truncate an identifier to a fixed prefix + ``…`` suffix."""
    if identifier is None:
        return "none"
    s = str(identifier)
    if len(s) <= length:
        return s
    half = length // 2
    return f"{s[:half]}…{s[-half:]}"


class Metrics:
    """In-process counter/histogram store.

    These are simple Python counters — they are NOT a Prometheus client.
    The gateway's metrics pipeline (if present) can read these or we
    can wire them to an external backend later.  For now, they provide
    structured accounting that tests can assert against.
    """

    _counters: Dict[str, int] = {}
    _histograms: Dict[str, list[float]] = {}

    @classmethod
    def increment(cls, name: str, labels: Optional[Dict[str, str]] = None) -> None:
        key = _metric_key(name, labels)
        cls._counters[key] = cls._counters.get(key, 0) + 1
        logger.debug("metric increment: %s -> %d", key, cls._counters[key])

    @classmethod
    def histogram(cls, name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        key = _metric_key(name, labels)
        bucket = cls._histograms.setdefault(key, [])
        bucket.append(value)
        if len(bucket) > 10000:
            # Cap to prevent unbounded growth
            cls._histograms[key] = bucket[-5000:]

    @classmethod
    def counter_value(cls, name: str, labels: Optional[Dict[str, str]] = None) -> int:
        return cls._counters.get(_metric_key(name, labels), 0)

    @classmethod
    def reset(cls) -> None:
        """Clear all metrics (use between tests)."""
        cls._counters.clear()
        cls._histograms.clear()

    @classmethod
    def snapshot(cls) -> Dict[str, Any]:
        """Return a snapshot of all metrics for inspection."""
        return {
            "counters": dict(cls._counters),
            "histograms": {k: list(v) for k, v in cls._histograms.items()},
        }


def _metric_key(name: str, labels: Optional[Dict[str, str]]) -> str:
    if not labels:
        return name
    parts = [f"{k}={v}" for k, v in sorted(labels.items())]
    return f"{name}[{','.join(parts)}]"


# Pre-defined metric names for consistency
METRIC_WEBHOOK_RECEIVED = "bytedance_webhook_received_total"
METRIC_WEBHOOK_DUPLICATE = "bytedance_webhook_duplicate_total"
METRIC_WEBHOOK_REJECTED = "bytedance_webhook_rejected_total"
METRIC_WEBHOOK_ACK_SECONDS = "bytedance_webhook_ack_seconds"
METRIC_MESSAGE_DISPATCH = "bytedance_message_dispatch_total"
METRIC_MESSAGE_SEND = "bytedance_message_send_total"
METRIC_POLICY_DECISION = "bytedance_policy_decision_total"
METRIC_API_REQUEST = "bytedance_api_request_total"
METRIC_API_LATENCY = "bytedance_api_latency_seconds"
METRIC_TOKEN_REFRESH = "bytedance_token_refresh_total"
METRIC_PUBLISH_INTENT = "bytedance_publish_intent_total"
METRIC_QUEUE_DEPTH = "bytedance_queue_depth"
