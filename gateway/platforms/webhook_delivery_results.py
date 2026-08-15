"""Typed fan-out accounting and exactly-once finalization coordination."""

from __future__ import annotations

import asyncio
import dataclasses
import time
from typing import Awaitable, Callable, Iterable


@dataclasses.dataclass(frozen=True)
class DeliveryOutcome:
    target: str
    success: bool
    error: str | None = None


@dataclasses.dataclass(frozen=True)
class FanoutResult:
    outcomes: tuple[DeliveryOutcome, ...]

    @property
    def any_success(self) -> bool:
        return any(item.success for item in self.outcomes)

    @property
    def all_success(self) -> bool:
        return bool(self.outcomes) and all(item.success for item in self.outcomes)

    @property
    def failures(self) -> tuple[DeliveryOutcome, ...]:
        return tuple(item for item in self.outcomes if not item.success)


async def run_fanout(
    deliveries: Iterable[tuple[str, Callable[[], Awaitable[object]]]],
) -> FanoutResult:
    """Attempt every target independently and retain every target outcome."""
    items = list(deliveries)
    if not items:
        return FanoutResult(())
    raw = await asyncio.gather(*(call() for _, call in items), return_exceptions=True)
    outcomes: list[DeliveryOutcome] = []
    for (target, _), value in zip(items, raw):
        if isinstance(value, BaseException):
            outcomes.append(DeliveryOutcome(target, False, str(value)))
            continue
        success = bool(getattr(value, "success", value is not False))
        error = getattr(value, "error", None) if not success else None
        outcomes.append(
            DeliveryOutcome(target, success, str(error) if error else None)
        )
    return FanoutResult(tuple(outcomes))


class CompletionOnce:
    """Serialize finalization once while keeping the dedup ledger bounded."""

    def __init__(self, *, ttl_seconds: float = 86400.0, max_entries: int = 8192):
        self._lock = asyncio.Lock()
        self._completed: dict[str, float] = {}
        self._ttl_seconds = max(1.0, float(ttl_seconds))
        self._max_entries = max(1, int(max_entries))

    def _prune_locked(self, now: float) -> None:
        cutoff = now - self._ttl_seconds
        for key, completed_at in list(self._completed.items()):
            if completed_at < cutoff:
                self._completed.pop(key, None)
        overflow = len(self._completed) - self._max_entries
        if overflow > 0:
            oldest = sorted(self._completed.items(), key=lambda item: item[1])
            for key, _ in oldest[:overflow]:
                self._completed.pop(key, None)

    async def run(
        self,
        key: str,
        action: Callable[[], Awaitable[None]],
    ) -> bool:
        now = time.monotonic()
        async with self._lock:
            self._prune_locked(now)
            if key in self._completed:
                return False
            # Mark before the external side effect. Retrying after an unknown
            # network outcome can duplicate a non-idempotent callback.
            self._completed[key] = now
            self._prune_locked(now)
        await action()
        return True
