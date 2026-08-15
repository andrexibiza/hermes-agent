"""Rate limiting utilities.

Per the design spec §3.1: rate limiting is isolated by profile and route,
then by provider/account where appropriate.  Uses a fixed-window
counter (per-route, per-account) — matching the webhook.py pattern.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Deque


class RateLimiter:
    """Fixed-window rate limiter keyed by arbitrary string.

    Each key gets its own deque of timestamps.  When the deque is full
    and the oldest entry is within the current window, the request
    is rejected.
    """

    def __init__(self, window_seconds: float = 60.0, max_requests: int = 30) -> None:
        self._window = window_seconds
        self._max = max_requests
        self._buckets: dict[str, Deque[float]] = {}

    def check(self, key: str, now: float | None = None) -> bool:
        """Return True if the request is allowed under the limit."""
        if now is None:
            now = time.time()
        cutoff = now - self._window
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = deque()
            self._buckets[key] = bucket

        # Evict expired entries
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

        if len(bucket) >= self._max:
            return False

        bucket.append(now)
        return True

    def remaining(self, key: str, now: float | None = None) -> int:
        """Return approximate remaining requests for a key."""
        if now is None:
            now = time.time()
        cutoff = now - self._window
        bucket = self._buckets.get(key)
        if bucket is None:
            return self._max
        # Count non-expired
        active = sum(1 for t in bucket if t >= cutoff)
        return max(0, self._max - active)
