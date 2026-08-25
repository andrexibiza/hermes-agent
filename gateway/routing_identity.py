"""Runtime authority for multiplexed transport resource claims.

Claim keys are useful indexes, but they are only a realization of an adapter's
identity at one instant.  They must not be passed around as proof that the same
adapter generation still owns the transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable


ClaimFactory = Callable[[Any, Any], tuple | None]


@dataclass(frozen=True, slots=True)
class TransportClaimantGeneration:
    """One live adapter generation and the profile on whose behalf it runs.

    The adapter object is deliberately the authority.  ``realize`` refuses a
    replacement object, even if it currently hashes to the same claim keys;
    callers must publish a new product for every recreated adapter.
    """

    profile: str
    platform: Any
    adapter: Any

    def realize(
        self,
        adapter: Any,
        factories: Iterable[ClaimFactory],
    ) -> tuple[tuple, ...]:
        if adapter is not self.adapter:
            return ()
        return tuple(
            claim
            for factory in factories
            if (claim := factory(self.platform, adapter)) is not None
        )
